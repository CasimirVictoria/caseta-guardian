#!/usr/bin/env python3
"""
Benchmark no invasiu de precisió local per a Cerbo GX / Caseta Guardian.
Mesura % CPU, kB/s escrits a Flash eMMC, canvis de context i memòria.
"""

import os
import sys
import time
import subprocess


def get_pid() -> int:
    try:
        out = subprocess.check_output(
            "svstat /service/caseta-guardian 2>/dev/null | grep -o 'pid [0-9]*' | awk '{print $2}'",
            shell=True
        ).decode().strip()
        if out.isdigit():
            return int(out)
    except Exception:
        pass
    return 17039


def read_proc(pid: int):
    try:
        with open(f"/proc/{pid}/stat", "r") as f:
            stat_line = f.read().split()
        utime = int(stat_line[13])
        stime = int(stat_line[14])

        write_bytes = 0
        syscw = 0
        with open(f"/proc/{pid}/io", "r") as f:
            for line in f:
                if line.startswith("write_bytes:"):
                    write_bytes = int(line.split()[1])
                elif line.startswith("syscw:"):
                    syscw = int(line.split()[1])

        rss_kb = 0
        vol_cs = 0
        nonvol_cs = 0
        with open(f"/proc/{pid}/status", "r") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    rss_kb = int(line.split()[1])
                elif line.startswith("voluntary_ctxt_switches:"):
                    vol_cs = int(line.split()[1])
                elif line.startswith("nonvoluntary_ctxt_switches:"):
                    nonvol_cs = int(line.split()[1])

        return {
            "time": time.time(),
            "cpu_ticks": utime + stime,
            "write_bytes": write_bytes,
            "syscw": syscw,
            "rss_kb": rss_kb,
            "cs": vol_cs + nonvol_cs,
        }
    except Exception as e:
        return None


def run(duration=30):
    pid = get_pid()
    print(f"📊 Mesurant Caseta Guardian (PID {pid}) a Cerbo GX durant {duration}s...")
    prev = read_proc(pid)
    if not prev:
        print("❌ Error llegint /proc.")
        return

    samples = []
    print("\n  Segon |  % CPU  |  Disc Escrit  |  Ops Escriptura  |   RAM RSS   | Canvis Context")
    print("  " + "-" * 75)

    for i in range(1, duration + 1):
        time.sleep(1.0)
        curr = read_proc(pid)
        if not curr:
            break

        dt = curr["time"] - prev["time"]
        d_ticks = curr["cpu_ticks"] - prev["cpu_ticks"]
        # USER_HZ a ARM Linux = 100 Hz
        cpu_pct = (d_ticks / 100.0) / dt * 100.0

        d_bytes = curr["write_bytes"] - prev["write_bytes"]
        kb_sec = (d_bytes / 1024.0) / dt

        d_syscw = curr["syscw"] - prev["syscw"]
        d_cs = curr["cs"] - prev["cs"]

        samples.append((cpu_pct, kb_sec, d_syscw, curr["rss_kb"], d_cs))
        print(f"  {i:5d} | {cpu_pct:6.1f}% | {kb_sec:9.2f} kB/s | {d_syscw:11d} op/s | {curr['rss_kb']/1024:8.1f} MB | {d_cs:7d} cs/s")
        prev = curr

    if samples:
        avg_cpu = sum(s[0] for s in samples) / len(samples)
        max_cpu = max(s[0] for s in samples)
        total_kb = sum(s[1] * 1.0 for s in samples)
        avg_cs = sum(s[4] for s in samples) / len(samples)
        print("  " + "=" * 75)
        print("  📈 RESUM LÍNIA BASE (ABANS):")
        print(f"     • CPU Mitjana: {avg_cpu:.2f}%  (Pic màxim: {max_cpu:.1f}%)")
        print(f"     • Total escrit a disc Flash: {total_kb:.2f} kB  ({total_kb/duration:.2f} kB/s mitjà)")
        print(f"     • Canvis de context mitjans: {avg_cs:.1f} cs/s")
        print(f"     • Memòria RAM utilitzada: {samples[-1][3]/1024:.1f} MB")


if __name__ == "__main__":
    dur = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    run(dur)
