"""唤醒故障诊断：只记录计数、耗时和状态，不保存语音、文本或设备命令正文。"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import traceback
from contextlib import contextmanager

from core.ref import get_app, get_kws, get_vad, get_xiaoai
from core.utils.logger import logger


class RuntimeDiagnostics:
    """音频线程只更新定长统计；独立线程写汇总，避免逐帧日志拖慢音频。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._timings = {}
        self._active = {}
        self._counters = {}
        self._events = {}
        self._fault_total = 0
        self._input = {}
        self._loops = {}
        self._last_warning = {}
        self._last_stack = 0.0
        self._started = time.monotonic()
        self._last_report = self._started
        self._last_cpu = time.process_time()

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="runtime-diagnostics")
        self._thread.start()
        logger.info(
            f"诊断已启用 pid={os.getpid()}：每60秒汇总，异常快照限频15秒，线程栈限频60秒；不改变检测策略",
            module="Diagnostics",
        )

    def stop(self):
        self._stop.set()

    @contextmanager
    def measure(self, name: str, audio_s: float = 0.0, warn_s: float | None = None):
        """记录完成耗时和仍未返回的调用；不同线程的同名调用互不覆盖。"""
        started = time.monotonic()
        clock_start = time.perf_counter()  # Windows 上 monotonic 可能只有约 16 ms 精度
        token = object()
        with self._lock:
            self._active[token] = (name, started)
        try:
            yield
        finally:
            elapsed = time.perf_counter() - clock_start
            with self._lock:
                self._active.pop(token, None)
                # n / 总耗时 / 最大耗时 / 超过音频时长的次数 / 音频总时长
                stat = self._timings.setdefault(name, [0, 0.0, 0.0, 0, 0.0])
                stat[0] += 1
                stat[1] += elapsed
                stat[2] = max(stat[2], elapsed)
                stat[3] += int(audio_s > 0 and elapsed > audio_s)
                stat[4] += audio_s
                if warn_s is not None and elapsed >= warn_s:
                    self._event_locked(name + ".slow", {"elapsed_ms": round(elapsed * 1000, 1)})

    def count(self, name: str, amount: int = 1):
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + amount

    def event(self, name: str, **fields):
        """调用方只传固定类别和数值；同类事件只保留次数与最后一次详情。"""
        with self._lock:
            self._event_locked(name, fields)

    def _event_locked(self, name, fields):
        if name.endswith((".slow", "_error", ".error", ".cancelled")) or name in {"kws.trim", "input.gap", "input.capture_changed", "input.watchdog_restart", "multimic.restart_requested", "multimic.eof"}:
            self._fault_total += 1
        old = self._events.get(name, {})
        self._events[name] = {
            "count": old.get("count", 0) + 1,
            "last_uptime_s": round(time.monotonic() - self._started, 3), "last": fields,
        }
        if "discarded_speech_s" in fields:
            self._events[name]["max_discarded_speech_s"] = max(old.get("max_discarded_speech_s", 0), fields["discarded_speech_s"])

    def input_packet(self, metadata: dict):
        """在主麦 DSP 前观察到包；ts_ms 仅用于同一采集内的相对延迟估计。"""
        now = time.monotonic()
        with self._lock:
            previous = self._input
            gap = now - previous.get("received_at", now)
            self._counters["input.packets"] = self._counters.get("input.packets", 0) + 1
            capture = metadata.get("capture_id")
            seq = metadata.get("seq")
            same = bool(capture and capture == previous.get("capture_id"))
            if same and isinstance(seq, int) and isinstance(previous.get("seq"), int):
                missing = max(0, seq - previous["seq"] - 1)
                if missing:
                    self._counters["input.missing_packets"] = self._counters.get("input.missing_packets", 0) + missing
            elif capture and previous.get("capture_id"):
                self._event_locked("input.capture_changed", {"capture_id": capture})
            delay = None
            baseline = previous.get("clock_offset_floor_ms") if same else None
            ts_ms = metadata.get("ts_ms")
            if isinstance(ts_ms, (int, float)):
                offset = now * 1000 - ts_ms
                baseline = offset if baseline is None else min(baseline, offset)
                delay = round(offset - baseline, 1)
            self._input = {
                "received_at": now, "capture_id": capture, "seq": seq,
                "clock_offset_floor_ms": baseline, "relative_delay_ms": delay,
                "max_gap_ms": max(previous.get("max_gap_ms", 0), round(gap * 1000, 1)),
            }
            if gap > 1:
                self._event_locked("input.gap", {"gap_ms": round(gap * 1000, 1), "capture_id": capture})

    def _heartbeat(self, name, sent_at):
        with self._lock:
            state = self._loops.get(name)
            if state and state["pending"] == sent_at:
                state["pending"] = None
                state["last_ack"] = time.monotonic()
                state["max_lag_ms"] = max(state["max_lag_ms"], (state["last_ack"] - sent_at) * 1000)

    def _probe_loops(self, now):
        app, xiaoai = get_app(), get_xiaoai()
        candidates = {"main": getattr(app, "loop", None), "bridge": getattr(xiaoai, "async_loop", None)}
        for name, loop in candidates.items():
            if loop is None or not loop.is_running():
                continue
            with self._lock:
                state = self._loops.setdefault(name, {"pending": None, "last_ack": now, "max_lag_ms": 0.0})
                if state["pending"] is not None:
                    continue  # 卡顿时最多保留一个探针，不能给主循环继续排队
                state["pending"] = now
            try:
                loop.call_soon_threadsafe(self._heartbeat, name, now)
            except RuntimeError:
                with self._lock:
                    self._loops.pop(name, None)

    @staticmethod
    def _audio_state(obj):
        if obj is None:
            return None
        stream = getattr(obj, "stream", None)
        thread = getattr(obj, "thread", None)
        return {
            "thread_alive": bool(thread and thread.is_alive()),
            "paused": obj.paused,
            "queued_ms": round(stream.unread_samples() / 16, 1) if stream else None,
            "stream_active": bool(stream and stream.is_active()),
        }

    def _snapshot(self, now, reset=False):
        # 只读长度和标志，不复制历史 PCM；这是跨线程近似快照，并非原子业务状态。
        app, xiaoai, vad, kws = get_app(), get_xiaoai(), get_vad(), get_kws()
        vad_state = self._audio_state(vad)
        if vad_state is not None:
            vad_state.update(
                target=vad.target, speech_s=round(len(vad.speech_frames) / 32000, 3),
                speech_storage_mb=round(sys.getsizeof(vad.speech_frames) / 1048576, 2),
                silence_s=round(len(vad.silence_frames) / 32000, 3),
            )
        kws_state = self._audio_state(kws)
        if kws_state is not None:
            kws_state.update(
                barge_in=bool(kws._barge_in_handler), hits=dict(kws.hit_counts),
                speech_gate={name: detector.active for name, detector in
                             [("ch0", kws.detector), *list(kws.extra_detectors.items())] if detector is not None},
            )
        multimic = getattr(kws, "multimic", None)
        multi_state = multimic.diagnostic_state(now) if multimic is not None else None
        last_input = getattr(xiaoai, "_last_input_at", None)
        with self._lock:
            packet = dict(self._input)
            packet.pop("clock_offset_floor_ms", None)
            received = packet.pop("received_at", None)
            packet["age_ms"] = round((now - received) * 1000, 1) if received else None
            timings = {
                name: {"n": s[0], "avg_ms": round(s[1] * 1000 / s[0], 2),
                       "max_ms": round(s[2] * 1000, 2), "over_budget": s[3],
                       "audio_s": round(s[4], 2)}
                for name, s in self._timings.items()
            }
            active = {}
            for name, started in self._active.values():
                active[name] = max(active.get(name, 0), round((now - started) * 1000, 1))
            loops = {
                name: {"pending_ms": round((now - s["pending"]) * 1000, 1) if s["pending"] else 0,
                       "max_lag_ms": round(s["max_lag_ms"], 1)}
                for name, s in self._loops.items()
            }
            counters, events = dict(self._counters), dict(self._events)
            cpu = time.process_time()
            elapsed = now - self._last_report
            cpu_pct = round((cpu - self._last_cpu) / max(elapsed, 0.001) * 100, 1)
            event_version = self._fault_total
            if reset:
                self._timings.clear()
                self._counters.clear()
                self._events.clear()
                self._input["max_gap_ms"] = 0
                for state in self._loops.values():
                    state["max_lag_ms"] = 0
                self._last_report, self._last_cpu = now, cpu
        return {
            "uptime_s": round(now - self._started, 1), "window_s": round(elapsed, 1),
            "cpu_core_pct": cpu_pct, "event_version": event_version,
            "device_state": str(getattr(app, "device_state", None)),
            "recording_wanted": getattr(xiaoai, "_recording_wanted", None),
            "delivered_age_ms": round((now - last_input) * 1000, 1) if last_input else None,
            "input": packet, "vad": vad_state, "kws": kws_state, "multimic": multi_state,
            "loops": loops, "active_ms": active, "timings": timings, "counts": counters, "events": events,
        }

    def _stacks(self):
        names = {t.ident: t.name for t in threading.enumerate()}
        stacks = {
            names.get(tid, str(tid)): [f"{f.filename}:{f.lineno}:{f.name}" for f in traceback.extract_stack(frame, limit=8)]
            for tid, frame in sys._current_frames().items()
            if tid != threading.get_ident()
        }
        logger.warning("线程栈 " + json.dumps(stacks, ensure_ascii=False), module="Diagnostics")

    def _run(self):
        last_tick = time.monotonic()
        seen_events = 0
        while not self._stop.wait(1):
            try:
                now = time.monotonic()
                observer_gap = now - last_tick
                last_tick = now
                self._probe_loops(now)
                periodic = now - self._last_report >= 60
                snapshot = self._snapshot(now, reset=periodic)
                reasons = []
                vad = snapshot["vad"]
                kws = snapshot["kws"]
                if vad and vad["thread_alive"] and not vad["paused"] and vad["target"] is None:
                    reasons.append("vad_running_without_target")
                if vad and getattr(get_vad(), "thread", None) and not vad["thread_alive"]:
                    reasons.append("vad_thread_dead")
                if vad and vad["speech_s"] > 30:
                    reasons.append("vad_history_over_30s")
                if kws and kws["queued_ms"] is not None and kws["queued_ms"] > 500:
                    reasons.append("kws_backlog_over_500ms")
                if kws and getattr(get_kws(), "thread", None) and not kws["thread_alive"]:
                    reasons.append("kws_thread_dead")
                if snapshot["recording_wanted"] and snapshot["input"]["age_ms"] is not None and snapshot["input"]["age_ms"] > 4000:
                    reasons.append("input_stale")
                loop_stall = any(s["pending_ms"] > 2000 or s["max_lag_ms"] > 2000 for s in snapshot["loops"].values())
                if loop_stall:
                    reasons.append("event_loop_stall")
                # 通用 shell 可能正在正常播放长 TTS；只对实时音频和停播链路自动抓栈。
                call_stall = any(ms > 2000 for name, ms in snapshot["active_ms"].items() if not name.startswith("rpc."))
                if call_stall:
                    reasons.append("pipeline_call_over_2s")
                if observer_gap > 2:
                    reasons.append("diagnostic_thread_delayed")
                with self._lock:
                    if self._fault_total != seen_events:
                        reasons.append("pipeline_events")
                fresh = [r for r in reasons if now - self._last_warning.get(r, 0) >= 15]
                if periodic or fresh:
                    snapshot["observer_gap_ms"] = round(observer_gap * 1000, 1)
                    snapshot["reasons"] = reasons
                    log = logger.warning if fresh else logger.info
                    log(("异常快照 " if fresh else "周期汇总 ") + json.dumps(snapshot, ensure_ascii=False), module="Diagnostics")
                    for reason in fresh:
                        self._last_warning[reason] = now
                    seen_events = snapshot["event_version"]
                if (loop_stall or observer_gap > 2 or call_stall) and now - self._last_stack >= 60:
                    self._last_stack = now
                    self._stacks()
            except Exception as exc:
                # 诊断失败不能影响音频线程或让监视线程静默退出。
                if time.monotonic() - self._last_warning.get("diagnostic_error", 0) >= 60:
                    self._last_warning["diagnostic_error"] = time.monotonic()
                    logger.warning(f"诊断采样失败: {type(exc).__name__}: {exc}", module="Diagnostics")


diagnostics = RuntimeDiagnostics()
