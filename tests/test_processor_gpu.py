import threading
import unittest
from datetime import datetime, timezone

from norev.core.models import Segment, SpeakerTurn, Transcript
from norev.processor.central import CentralProcessor, GpuGate
from norev.processor.identity import LocalUser
from norev.testing.fakes import FakeLlmClient, FakeTranscriber, FixedClock

WAIT = 5  # seconds; only reached if the gate deadlocks


class GpuGateTest(unittest.TestCase):
    def test_previous_model_is_released_before_the_next_runs(self) -> None:
        gate, order = GpuGate(), []
        with gate.use("whisper", lambda: order.append("release whisper")):
            order.append("whisper")
        with gate.use("llm:qwen", lambda: order.append("release qwen")):
            order.append("qwen")
        with gate.use("llm:qwen"):  # same model: no release
            order.append("qwen")
        self.assertEqual(order, ["whisper", "release whisper", "qwen", "qwen"])

    def test_calls_for_the_same_model_run_together(self) -> None:
        gate, both_inside = GpuGate(), threading.Barrier(2, timeout=WAIT)

        def call() -> None:
            with gate.use("llm:qwen"):
                both_inside.wait()  # breaks (BrokenBarrierError) unless both are inside at once

        threads = [threading.Thread(target=call) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(WAIT)
        self.assertFalse(both_inside.broken)

    def test_another_model_waits_and_is_not_starved(self) -> None:
        gate, order = GpuGate(), []
        qwen_inside, let_qwen_finish = threading.Event(), threading.Event()

        def first_qwen() -> None:
            with gate.use("llm:qwen", lambda: order.append("release qwen")):
                qwen_inside.set()
                let_qwen_finish.wait(WAIT)
                order.append("qwen 1 done")

        def whisper() -> None:
            with gate.use("whisper"):
                order.append("whisper")

        def second_qwen() -> None:
            with gate.use("llm:qwen"):
                order.append("qwen 2")

        threads = [threading.Thread(target=first_qwen)]
        threads[0].start()
        qwen_inside.wait(WAIT)
        threads.append(threading.Thread(target=whisper))
        threads[1].start()
        self._wait_until_waiting(gate, "whisper")
        threads.append(threading.Thread(target=second_qwen))  # arrives after whisper asked for the GPU
        threads[2].start()
        let_qwen_finish.set()
        for t in threads:
            t.join(WAIT)
        self.assertEqual(order, ["qwen 1 done", "release qwen", "whisper", "qwen 2"])

    def test_failed_release_does_not_run_the_next_model(self) -> None:
        gate = GpuGate()

        def release() -> None:
            raise RuntimeError("CUDA error")

        with gate.use("whisper", release):
            pass
        with self.assertRaises(RuntimeError):
            with gate.use("llm:qwen"):
                self.fail("ran while Whisper still held the GPU")
        with gate.use("whisper"):  # the gate is not stuck
            pass

    @staticmethod
    def _wait_until_waiting(gate: GpuGate, name: str) -> None:
        for _ in range(WAIT * 100):
            with gate._cond:
                if gate._next == name:
                    return
            threading.Event().wait(0.01)
        raise AssertionError(f"{name} never waited for the GPU")


class ClosingTranscriber(FakeTranscriber):
    def __init__(self, order: list, transcripts: dict) -> None:
        super().__init__(transcripts)
        self.order = order

    def transcribe(self, audio_ref, language=None):
        self.order.append("transcribe")
        return super().transcribe(audio_ref, language)

    def close(self) -> None:
        self.order.append("close transcriber")


class ClosingDiarizer:
    def __init__(self, order: list) -> None:
        self.order = order

    def diarize(self, audio_ref):
        self.order.append("diarize")
        return [SpeakerTurn(0.0, 1.0, "S1")]

    def close(self) -> None:
        self.order.append("close diarizer")


class ProcessorGpuOrderTest(unittest.TestCase):
    def test_whisper_diarization_and_llm_take_turns_on_the_gpu(self) -> None:
        order: list[str] = []
        transcript = Transcript((Segment(0.0, 1.0, "Bună ziua."),), language="ro")
        llm = FakeLlmClient(['{"ok": true}'])
        processor = CentralProcessor(
            llm,
            audit=None,  # no sends in this test
            identity=LocalUser("u"),
            clock=FixedClock(datetime(2026, 11, 6, tzinfo=timezone.utc)),
            transcriber=ClosingTranscriber(order, {"a1": transcript}),
            diarizer=ClosingDiarizer(order),
        )

        self.assertEqual(processor.transcribe("a1", "ro"), transcript)
        self.assertEqual(processor.diarize("a1"), [SpeakerTurn(0.0, 1.0, "S1")])
        self.assertEqual(processor.generate_json("p", model="qwen"), '{"ok": true}')
        self.assertEqual(order, ["transcribe", "close transcriber", "diarize", "close diarizer"])
        self.assertEqual(len(llm.calls), 1)

    def test_missing_transcriber_or_diarizer_is_an_error_not_a_default(self) -> None:
        processor = CentralProcessor(FakeLlmClient([]), None, identity=LocalUser("u"), clock=FixedClock(None))
        with self.assertRaises(RuntimeError):
            processor.transcribe("a1")
        with self.assertRaises(RuntimeError):
            processor.diarize("a1")


if __name__ == "__main__":
    unittest.main()
