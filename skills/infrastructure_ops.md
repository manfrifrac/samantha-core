# Global Skills — Infrastructure, Services & Resilience (`infrastructure`)

> **Domain**: Linux daemon orchestration, self-healing architectures, OOM protection, atomic process locks, Git security, quota probes, and process group isolation.  
> **Source Reference**: Production supervisor implementations, watchdog daemons, and system resilience post-mortems across the Samantha Ecosystem.

---

## 1. Declarative & Topological Process Supervisor (`supervisore_unico.py`)

- **Problem Solved**:
  System microservices fragmented across divergent shell scripts, orphan tmux panes, and manual spawns. This generated port conflicts, duplicate daemon executions, and unmonitored zombie processes.
- **Technical Explanation**:
  A unified three-module architecture provides clean separation of concerns and a declarative single source of truth:
  1. `supervisore_config.py`: Declarative service registry defining runtimes (Python/Node/binary), listening ports, working directories, topological startup dependencies (tiers 1–3), health thresholds, and OOM priorities.
  2. `supervisore_stato.py`: Persistent state management backed by atomic `flock` file locking and non-invasive health probes (TCP socket connect, HTTP status checks, PID liveness).
  3. `supervisore_unico.py`: Core orchestrator managing topological cascade startups, dedicated POSIX Process Group isolation (`os.setsid`), centralized log multiplexing with automated rotation, and an administrative CLI.
- **Implementation Guide**:
  - Check system status:
    ```bash
    python3 core/supervisore_unico.py status
    ```
  - Restart a specific service:
    ```bash
    python3 core/supervisore_unico.py restart <service_name>
    ```
  - Tail aggregate logs:
    ```bash
    python3 core/supervisore_unico.py logs [service_name] -n 50
    ```

---

## 2. Single-Instance Daemon Protection with Atomic File Locks (`flock`)

- **Problem Solved**:
  Periodic tasks or background daemons (e.g., dispatchers, autopilots, synchronization crons) being triggered concurrently (e.g., overlapping cron ticks or duplicate system restarts), causing database corruption and duplicate message dispatching.
- **Technical Explanation**:
  Kernel-level advisory file locking (`flock`) guarantees that only a single instance acquires an exclusive non-blocking file descriptor. Upon process termination (including fatal crashes or SIGKILL), the Linux kernel automatically releases the lock.
- **Implementation Guide**:
  - **In Python** (at entry point of `main()`):
    ```python
    import fcntl, sys, os

    def acquire_single_instance(lock_path: str):
        try:
            lock_file = open(lock_path, "w")
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return lock_file
        except (IOError, BlockingIOError):
            print(f"[LOCK] Another instance of {os.path.basename(sys.argv[0])} is already active. Exiting.")
            sys.exit(0)
    ```
  - **In Bash**:
    ```bash
    flock -n /tmp/mydaemon.lock -c "python3 core/mydaemon.py"
    ```

---

## 3. Kernel OOM-Killer Protection for Critical Daemons (`oom_score_adj = -500`)

- **Problem Solved**:
  During transient memory spikes (e.g., intensive compilation builds or bulk data indexing), the Linux kernel Out-Of-Memory (OOM) killer abruptly terminates core ecosystem infrastructure such as the LiteLLM router, Spend Guard, or the Supervisor.
- **Technical Explanation**:
  The Linux kernel computes an `oom_score` to prioritize processes for termination during memory starvation. Writing a negative adjustment value to `/proc/<PID>/oom_score_adj` (ranging from -1 to -1000) significantly lowers the kill priority.
- **Implementation Guide**:
  At daemon startup (executed as root or with `CAP_SYS_RESOURCE`):
  ```python
  def protect_against_oom(score: int = -500):
      try:
          with open("/proc/self/oom_score_adj", "w") as f:
              f.write(str(score))
      except Exception as e:
          print(f"[WARN] Failed to set oom_score_adj: {e}")
  ```

---

## 4. Process Group Isolation for Child Subprocesses (`os.setsid`)

- **Problem Solved**:
  When a supervisor manages external processes (e.g., headless browsers, LiteLLM instances, Node bridges), terminating or restarting the service leaves orphaned grand-child processes holding open TCP sockets.
- **Technical Explanation**:
  Spawning subprocesses with `preexec_fn=os.setsid` creates a new POSIX session and process group. Sending signals to `-os.getpgid(pid)` guarantees that the entire process tree receives termination signals (`SIGTERM` / `SIGKILL`).
- **Implementation Guide**:
  - **Spawning Process**:
    ```python
    import subprocess, os

    proc = subprocess.Popen(
        cmd,
        cwd=work_dir,
        preexec_fn=os.setsid,
        stdout=log_file,
        stderr=subprocess.STDOUT
    )
    ```
  - **Terminating Process Group**:
    ```python
    import os, signal

    def kill_process_tree(pid: int):
        try:
            pgid = os.getpgid(pid)
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    ```

---

## 5. Non-Blocking Real-Interval CPU & Memory Probing

- **Problem Solved**:
  Monitoring scripts querying `psutil.cpu_percent()` with blocking sleep intervals causing telemetry loop stalls, or calling `psutil.cpu_percent(interval=None)` which returns 0.0 or inaccurate instantaneous values.
- **Technical Explanation**:
  Accurate CPU utilization requires computing time deltas between two reads of `/proc/stat` or maintaining non-blocking timestamp counters across iteration loops.
- **Implementation Guide**:
  ```python
  import time, psutil

  class NonBlockingResourceMonitor:
      def __init__(self):
          self.last_check = time.time()
          psutil.cpu_percent(interval=None) # Initialize baseline

      def get_metrics(self):
          now = time.time()
          elapsed = now - self.last_check
          self.last_check = now
          cpu = psutil.cpu_percent(interval=None)
          mem = psutil.virtual_memory().percent
          return {"cpu_percent": cpu, "mem_percent": mem, "sample_window_sec": elapsed}
  ```

---

## 🎯 Model Routing Recommendations

- **Primary Engine**: **Claude 3.7 Sonnet / Opus (`claude` CLI)**
  - Essential for zero-error execution in Linux OS system calls, POSIX process management, and networking configurations.
- **Secondary Engine**: **Gemini 2.5 Pro (`agy` CLI)**
  - Suitable for multi-daemon log auditing and topological dependency graph synthesis.
