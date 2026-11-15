// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

//! Experimental full-memory capture. The caller keeps the VM paused until its disk cut.
//! A single worker owns both the page scanner and fault handler, so a saved page can never
//! be overwritten by a later fault. No worker operation needs the VMM lock.

use std::fs::{self, File, OpenOptions};
use std::io;
use std::os::unix::fs::FileExt;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::Instant;

use bitvec::vec::BitVec;
use serde::Serialize;
use userfaultfd::{Event, FaultKind, FeatureFlags, IoctlFlags, RegisterMode, Uffd, UffdBuilder};
use vm_memory::GuestMemoryBackend;

use crate::vmm_config::snapshot::{CreateSnapshotParams, SnapshotType};
use crate::vstate::memory::{Bytes, GuestAddress, GuestMemoryMmap, GuestMemoryRegion};

const PAGE: usize = 4096;
const BATCH: usize = 64 * 1024;
// Linux uapi/linux/userfaultfd.h; not yet named by userfaultfd-rs 0.9.
const WP_UNPOPULATED: u64 = 1 << 13;

/// Status of the last capture. Complete means the requested file sync has finished.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize)]
pub struct SnapshotStatus {
    /// Idle, Running, Complete, Failed, or Aborted.
    pub state: SnapshotState,
    /// Bytes already owned by the memory output.
    pub copied_bytes: u64,
    /// Total memory file size.
    pub total_bytes: u64,
    /// Number of write-protection events serviced.
    pub wp_faults: u64,
    /// Worker duration, fixed at termination.
    pub elapsed_ms: u64,
    /// Failure detail, including cleanup errors.
    pub error: Option<String>,
}

/// Lifecycle of the most recent capture; only Complete is loadable.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize)]
pub enum SnapshotState {
    /// No capture was requested.
    #[default]
    Idle,
    /// Write protection is armed and copying is in progress.
    Running,
    /// All bytes and the requested fsync completed.
    Complete,
    /// Capture failed; its output must not be loaded.
    Failed,
    /// Caller cancelled capture; its output must not be loaded.
    Aborted,
}

/// Owns the lifetime of a capture without exposing page-management details to callers.
#[derive(Debug, Default)]
pub(crate) struct BackgroundSnapshot {
    enabled: bool,
    status: Arc<Mutex<SnapshotStatus>>,
    cancel: Arc<AtomicBool>,
    worker: Option<JoinHandle<()>>,
    paths: Option<(PathBuf, PathBuf)>,
}

impl BackgroundSnapshot {
    pub(crate) fn new(enabled: bool) -> Self {
        let mut capture = Self::default();
        capture.enabled = enabled;
        capture
    }

    pub(crate) fn status(&self) -> SnapshotStatus {
        self.status
            .lock()
            .expect("panic=abort prevents status lock poisoning")
            .clone()
    }

    pub(crate) fn ensure_idle(&self) -> io::Result<()> {
        if self
            .status
            .lock()
            .expect("panic=abort prevents status lock poisoning")
            .state
            == SnapshotState::Running
        {
            return Err(io::Error::other("a background snapshot is already running"));
        }
        Ok(())
    }

    // Detach a prior generation even if validation of the next request fails.
    pub(crate) fn prepare_next(&mut self, kind: SnapshotType) -> io::Result<()> {
        self.ensure_idle()?;
        self.stop();
        self.paths = None;
        if kind == SnapshotType::Background {
            self.ensure_enabled()?;
        }
        Ok(())
    }

    fn ensure_enabled(&self) -> io::Result<()> {
        if !self.enabled {
            return Err(io::Error::other(
                "experimental Background snapshots require --no-seccomp",
            ));
        }
        Ok(())
    }

    pub(crate) fn start(
        &mut self,
        memory: GuestMemoryMmap,
        params: &CreateSnapshotParams,
    ) -> io::Result<()> {
        self.ensure_idle()?;
        self.ensure_enabled()?;
        self.stop();
        let file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&params.mem_file_path)?;
        let prepared = Capture::prepare(memory, file);
        let capture = match prepared {
            Ok(capture) => capture,
            Err(err) => {
                return Err(cleanup_error(err, &[&params.mem_file_path]));
            }
        };
        let total_bytes = capture.regions.iter().map(|r| r.len as u64).sum();
        self.status = Arc::new(Mutex::new(SnapshotStatus {
            state: SnapshotState::Running,
            total_bytes,
            ..SnapshotStatus::default()
        }));
        self.cancel = Arc::new(AtomicBool::new(false));
        let cancel = self.cancel.clone();
        let status = self.status.clone();
        let paths = (params.mem_file_path.clone(), params.snapshot_path.clone());
        self.paths = Some(paths.clone());
        let sync = params.sync_snapshot_files;
        match thread::Builder::new()
            .name("fc_bg_snapshot".into())
            .spawn(move || {
                let started = Instant::now();
                let mut capture = capture;
                let result = capture.run(&cancel, &status, sync);
                // Closing the last UFFD unregisters every range and wakes blocked writers,
                // including after a write, ioctl, or cancellation failure.
                drop(capture);
                let mut current = status
                    .lock()
                    .expect("panic=abort prevents status lock poisoning");
                current.elapsed_ms = started.elapsed().as_millis() as u64;
                current.state = match &result {
                    Ok(()) => SnapshotState::Complete,
                    Err(_) if cancel.load(Ordering::Acquire) => SnapshotState::Aborted,
                    Err(_) => SnapshotState::Failed,
                };
                current.error = result.err().map(|err| err.to_string());
                if current.state != SnapshotState::Complete {
                    if let Err(err) = remove_outputs(&[&paths.0, &paths.1]) {
                        current.error = Some(format!("{:?}; {err}", current.error));
                    }
                }
            }) {
            Ok(worker) => self.worker = Some(worker),
            Err(err) => {
                // spawn drops its closure (and hence UFFD) on failure.
                *self
                    .status
                    .lock()
                    .expect("panic=abort prevents status lock poisoning") =
                    SnapshotStatus::default();
                return Err(cleanup_error(err, &[&params.mem_file_path]));
            }
        }
        Ok(())
    }

    /// Stop before joining vCPUs: they may currently be waiting on this worker.
    pub(crate) fn stop(&mut self) {
        self.cancel.store(true, Ordering::Release);
        if let Some(worker) = self.worker.take() {
            // panic=abort in Firecracker; a worker panic cannot leave a running VM stranded.
            let _ = worker.join();
        }
    }

    pub(crate) fn abort(&mut self) {
        self.stop();
        if let Some(paths) = self.paths.take() {
            let mut status = self
                .status
                .lock()
                .expect("panic=abort prevents status lock poisoning");
            status.state = SnapshotState::Aborted;
            if let Err(err) = remove_outputs(&[&paths.0, &paths.1]) {
                status.error = Some(err.to_string());
                status.state = SnapshotState::Failed;
            }
        }
    }
}

impl Drop for BackgroundSnapshot {
    fn drop(&mut self) {
        self.stop();
    }
}

fn remove_outputs(paths: &[&std::path::Path]) -> io::Result<()> {
    let mut failure = None;
    for path in paths {
        if let Err(err) = fs::remove_file(path)
            && err.kind() != io::ErrorKind::NotFound
        {
            failure = Some(io::Error::other(format!(
                "cleanup {}: {err}",
                path.display()
            )));
        }
    }
    failure.map_or(Ok(()), Err)
}

fn cleanup_error(error: io::Error, paths: &[&std::path::Path]) -> io::Error {
    match remove_outputs(paths) {
        Ok(()) => error,
        Err(cleanup) => io::Error::other(format!("{error}; {cleanup}")),
    }
}

struct Region {
    host: usize,
    guest: u64,
    len: usize,
    file_offset: u64,
    copied: BitVec,
}

struct Capture {
    uffd: Uffd,
    memory: GuestMemoryMmap,
    file: File,
    regions: Vec<Region>,
}

impl Capture {
    fn prepare(memory: GuestMemoryMmap, file: File) -> io::Result<Self> {
        let uffd = UffdBuilder::new()
            .close_on_exec(true)
            .non_blocking(true)
            .user_mode_only(false)
            .require_features(
                FeatureFlags::PAGEFAULT_FLAG_WP | FeatureFlags::from_bits_retain(WP_UNPOPULATED),
            )
            .create()
            .map_err(io::Error::other)?;
        let mut regions = Vec::new();
        let mut file_offset = 0;
        for region in memory.iter() {
            let len = region.len() as usize;
            if region.file_offset().is_some() || region.flags() & libc::MAP_ANONYMOUS == 0 {
                return Err(io::Error::other(
                    "Background requires anonymous guest RAM; private regular-file mmap is not synchronous UFFD-WP compatible",
                ));
            }
            let host = region.as_ptr() as usize;
            let ioctls = uffd
                .register_with_mode(host as *mut _, len, RegisterMode::WRITE_PROTECT)
                .map_err(io::Error::other)?;
            if !ioctls.contains(IoctlFlags::WRITE_PROTECT) {
                return Err(io::Error::other(
                    "guest RAM does not support UFFDIO_WRITEPROTECT",
                ));
            }
            regions.push(Region {
                host,
                guest: region.start_addr().0,
                len,
                file_offset,
                copied: BitVec::repeat(false, len / PAGE),
            });
            file_offset += len as u64;
        }
        file.set_len(file_offset)?;
        for region in &regions {
            uffd.write_protect(region.host as *mut _, region.len)
                .map_err(io::Error::other)?;
        }
        Ok(Self {
            uffd,
            memory,
            file,
            regions,
        })
    }

    fn save_range(
        &mut self,
        region: usize,
        first: usize,
        count: usize,
        buffer: &mut [u8],
    ) -> io::Result<u64> {
        let r = &self.regions[region];
        let offset = first * PAGE;
        let len = count * PAGE;
        // vm-memory performs volatile accesses; every page in this range is still WP.
        self.memory
            .read_slice(&mut buffer[..len], GuestAddress(r.guest + offset as u64))
            .map_err(io::Error::other)?;
        self.file
            .write_all_at(&buffer[..len], r.file_offset + offset as u64)?;
        self.regions[region].copied[first..first + count].fill(true);
        self.uffd
            .remove_write_protection((self.regions[region].host + offset) as *mut _, len, true)
            .map_err(io::Error::other)?;
        Ok(len as u64)
    }

    fn run(
        &mut self,
        cancel: &AtomicBool,
        status: &Mutex<SnapshotStatus>,
        sync: bool,
    ) -> io::Result<()> {
        let mut buffer = vec![0; BATCH];
        let mut region = 0;
        let mut page = 0;
        let mut copied_bytes = 0;
        let mut wp_faults = 0;
        while region < self.regions.len() {
            if cancel.load(Ordering::Acquire) {
                return Err(io::Error::new(
                    io::ErrorKind::Interrupted,
                    "background snapshot aborted",
                ));
            }
            // Bound fault processing so an intensive writer cannot starve the scanner.
            for _ in 0..64 {
                let Some(event) = self.uffd.read_event().map_err(io::Error::other)? else {
                    break;
                };
                let Event::Pagefault {
                    kind: FaultKind::WriteProtected,
                    addr,
                    ..
                } = event
                else {
                    return Err(io::Error::other(format!(
                        "unexpected UFFD event: {event:?}"
                    )));
                };
                let host = addr as usize & !(PAGE - 1);
                let index = self
                    .regions
                    .iter()
                    .position(|r| host >= r.host && host - r.host < r.len)
                    .ok_or_else(|| io::Error::other("UFFD event outside guest RAM"))?;
                let fault_page = (host - self.regions[index].host) / PAGE;
                if !self.regions[index].copied[fault_page] {
                    copied_bytes += self.save_range(index, fault_page, 1, &mut buffer)?;
                }
                wp_faults += 1;
            }
            let pages = self.regions[region].len / PAGE;
            while page < pages && self.regions[region].copied[page] {
                page += 1;
            }
            if page == pages {
                region += 1;
                page = 0;
                continue;
            }
            let mut count = 1;
            while count < BATCH / PAGE
                && page + count < pages
                && !self.regions[region].copied[page + count]
            {
                count += 1;
            }
            copied_bytes += self.save_range(region, page, count, &mut buffer)?;
            page += count;
            let mut current = status
                .lock()
                .expect("panic=abort prevents status lock poisoning");
            current.copied_bytes = copied_bytes;
            current.wp_faults = wp_faults;
        }
        {
            let mut current = status
                .lock()
                .expect("panic=abort prevents status lock poisoning");
            current.copied_bytes = copied_bytes;
            current.wp_faults = wp_faults;
        }
        if sync {
            self.file.sync_all()?;
        }
        Ok(())
    }
}
