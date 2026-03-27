from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import traceback
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from predict import Output, Predictor


@contextlib.contextmanager
def pushd(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def _serialize_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, list):
        return [_serialize_value(item) for item in value]
    return str(value)


def _build_predictor_kwargs(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "music_input": Path(payload["music_input"]),
        "visualize": bool(payload.get("visualize", False)),
        "sonify": bool(payload.get("sonify", False)),
        "model": payload.get("model", "harmonix-all"),
        "include_activations": bool(payload.get("include_activations", False)),
        "include_embeddings": bool(payload.get("include_embeddings", False)),
        "audioSeparator": bool(payload.get("audioSeparator", False)),
        "audioSeparatorModel": payload.get("audioSeparatorModel", "Kim_Vocal_2.onnx"),
        "includeMdxOutputs": bool(payload.get("includeMdxOutputs", False)),
    }


def _serialize_output(output: Output) -> dict[str, Any]:
    return {
        field_name: _serialize_value(getattr(output, field_name, None))
        for field_name in Output.__annotations__
    }


def _run_prediction(predictor: Predictor, workspace: Path, payload: dict[str, Any]) -> dict[str, Any]:
    workspace.mkdir(parents=True, exist_ok=True)
    logs = io.StringIO()
    try:
        with pushd(workspace), contextlib.redirect_stdout(logs), contextlib.redirect_stderr(logs):
            output = predictor.predict(**_build_predictor_kwargs(payload))
        return {
            "ok": True,
            "output": _serialize_output(output),
            "logs": logs.getvalue(),
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "logs": logs.getvalue() + traceback.format_exc(),
        }


def _emit(message: dict[str, Any]) -> None:
    print(json.dumps(message), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run predictor requests.")
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--payload-json", type=Path)
    parser.add_argument("--manifest-json", type=Path)
    parser.add_argument("--setup-only", action="store_true")
    parser.add_argument("--server", action="store_true")
    args = parser.parse_args()

    predictor = Predictor()
    setup_logs = io.StringIO()
    try:
        with contextlib.redirect_stdout(setup_logs), contextlib.redirect_stderr(setup_logs):
            predictor.setup()
    except Exception as exc:
        failure = {
            "type": "ready",
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "logs": setup_logs.getvalue() + traceback.format_exc(),
        }
        if args.server:
            _emit(failure)
            return 1
        sys.stderr.write(failure["logs"])
        return 1

    if args.setup_only:
        return 0

    if args.server:
        _emit({"type": "ready", "ok": True, "logs": setup_logs.getvalue()})
        for line in sys.stdin:
            if not line.strip():
                continue
            try:
                request = json.loads(line)
                workspace = Path(request["workspace"])
                payload = request["payload"]
                response = _run_prediction(predictor, workspace, payload)
            except Exception as exc:
                response = {
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "logs": traceback.format_exc(),
                }
            response["type"] = "response"
            _emit(response)
        return 0

    if not args.workspace or not args.payload_json or not args.manifest_json:
        parser.error("--workspace, --payload-json, and --manifest-json are required unless --setup-only or --server is used.")

    payload = json.loads(args.payload_json.read_text())
    response = _run_prediction(predictor, args.workspace, payload)
    if not response["ok"]:
        sys.stderr.write(str(response.get("logs", "")))
        return 1

    args.manifest_json.write_text(json.dumps({"output": response["output"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
