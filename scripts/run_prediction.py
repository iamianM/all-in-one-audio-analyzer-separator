from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from predict import Output, Predictor


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
    }


def _serialize_output(output: Output) -> dict[str, Any]:
    return {
        field_name: _serialize_value(getattr(output, field_name, None))
        for field_name in Output.__annotations__
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a single predictor invocation.")
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--payload-json", type=Path)
    parser.add_argument("--manifest-json", type=Path)
    parser.add_argument("--setup-only", action="store_true")
    args = parser.parse_args()

    predictor = Predictor()
    predictor.setup()

    if args.setup_only:
        return 0

    if not args.workspace or not args.payload_json or not args.manifest_json:
        parser.error("--workspace, --payload-json, and --manifest-json are required unless --setup-only is used.")

    payload = json.loads(args.payload_json.read_text())
    args.workspace.mkdir(parents=True, exist_ok=True)
    os.chdir(args.workspace)

    output = predictor.predict(**_build_predictor_kwargs(payload))
    args.manifest_json.write_text(json.dumps({"output": _serialize_output(output)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
