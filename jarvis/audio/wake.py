"""Wake-word detection with openWakeWord's pretrained "hey jarvis" model.

Runs entirely offline on 80 ms frames. Two consecutive frames over the threshold are
required, which kills almost all false activations from TV chatter and clicks.
"""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

log = logging.getLogger("audio.wake")

_CANDIDATE_SUFFIXES = ("", "_v0.1", "_v0.2")


def _install_sklearn_stub() -> None:
    """openWakeWord imports scikit-learn at package import time, only for its custom
    verifier *training* helper. sklearn costs ~31 s to import on this laptop and is
    never used at runtime, so we satisfy the import with a tiny stub. If the real
    package is already loaded, do nothing.
    """
    import sys
    import types

    if "sklearn" in sys.modules:
        return

    sklearn = types.ModuleType("sklearn")
    linear_model = types.ModuleType("sklearn.linear_model")
    pipeline = types.ModuleType("sklearn.pipeline")
    preprocessing = types.ModuleType("sklearn.preprocessing")

    class _Unused:  # pragma: no cover - never instantiated at runtime
        def __init__(self, *args, **kwargs) -> None:
            raise RuntimeError("scikit-learn is not available; training is unsupported")

    class LogisticRegression(_Unused):
        pass

    def make_pipeline(*args, **kwargs):  # noqa: ANN001, ANN201
        raise RuntimeError("scikit-learn is not available; training is unsupported")

    linear_model.LogisticRegression = LogisticRegression
    pipeline.make_pipeline = make_pipeline
    preprocessing.FunctionTransformer = _Unused
    preprocessing.StandardScaler = _Unused

    sklearn.linear_model = linear_model
    sklearn.pipeline = pipeline
    sklearn.preprocessing = preprocessing
    sys.modules["sklearn"] = sklearn
    sys.modules["sklearn.linear_model"] = linear_model
    sys.modules["sklearn.pipeline"] = pipeline
    sys.modules["sklearn.preprocessing"] = preprocessing


class WakeWord:
    def __init__(self, name: str = "hey_jarvis", threshold: float = 0.55,
                 frames: int = 2) -> None:
        _install_sklearn_stub()
        from openwakeword.model import Model  # imported late so --devices works without it

        self.threshold = threshold
        self.required = max(1, frames)
        self.hits = 0
        self.last_score = 0.0
        self.model_name = name

        model = None
        errors: list[str] = []
        bundled = self._bundled_models()
        for candidate in self._candidates(name, bundled):
            try:
                model = Model(wakeword_models=[candidate], inference_framework="onnx")
                self.model_name = candidate
                break
            except Exception as exc:  # noqa: BLE001 - try the next spelling
                errors.append(f"{candidate}: {exc}")
        if model is None:
            raise RuntimeError(
                "could not load wake word model. Tried: " + "; ".join(errors)
            )
        self.model = model
        log.info("wake word ready: '%s' (threshold %.2f, %d frames)", self.model_name,
                 self.threshold, self.required)

    @staticmethod
    def _bundled_models() -> list[str]:
        try:
            import openwakeword
            root = Path(openwakeword.__file__).parent / "resources" / "models"
            if root.is_dir():
                return [p.stem for p in root.glob("*.onnx")]
        except Exception:  # noqa: BLE001
            pass
        return []

    @staticmethod
    def _candidates(name: str, bundled: list[str]) -> list[str]:
        out: list[str] = []
        for suffix in _CANDIDATE_SUFFIXES:
            candidate = name + suffix
            if not bundled or candidate in bundled:
                out.append(candidate)
        # if the requested name is missing, search the bundled ones for a match
        if not out:
            out = [b for b in bundled if name.split("_")[0] in b] or bundled[:1]
        return out

    def detect(self, block: np.ndarray, threshold: float | None = None) -> bool:
        """Return True exactly once per activation.

        `threshold` can be raised by the caller while Jarvis is speaking, so its own
        voice coming back through the speakers cannot trigger a barge-in.
        """
        limit = self.threshold if threshold is None else threshold
        try:
            scores = self.model.predict(block)
        except Exception as exc:  # noqa: BLE001 - never let the audio loop die
            log.debug("wake predict failed: %s", exc)
            return False
        score = max(scores.values()) if scores else 0.0
        self.last_score = float(score)
        if score >= limit:
            self.hits += 1
        else:
            self.hits = 0
        if self.hits >= self.required:
            self.hits = 0
            self.model.reset()
            log.info("wake word detected (score %.2f)", score)
            return True
        return False

    def reset(self) -> None:
        self.hits = 0
        try:
            self.model.reset()
        except Exception:  # noqa: BLE001
            pass
