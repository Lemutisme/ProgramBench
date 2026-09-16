import json

import pytest

from scripts.growth_loop import GrowthError, encode, generator_context_input, sha256
from scripts.growth_native import generator_context


def study(tmp_path):
    context = tmp_path / "context.txt"
    context.write_text("Closed development evidence.\n")
    builder = tmp_path / "builder.ts"
    builder.write_text('Bun.build({define:{"process.env.OPENCODE_ACTION_FUSION":JSON.stringify("0")}})\n')
    private = tmp_path / "qualification.py"
    private.write_text("PRIVATE_QUALIFICATION_SENTINEL")
    def reference(path):
        return {"path": str(path), "sha256": sha256(path.read_bytes())}
    return {"version": 2, "generator": {"context": reference(context), "build_context_dependencies": ["builder.ts"]},
        "revision_recipe": {"build": {"argv": ["bun", "/dependencies/builder.ts"], "timeout_seconds": 1200},
            "dependencies": {"builder.ts": reference(builder), "qualification.py": reference(private)},
            "qualifications": [{"script": reference(private)}]}}


def test_actual_admission_context_exposes_selected_fixed_define_without_unselected_contents(tmp_path):
    value = study(tmp_path)
    data = generator_context_input(value)
    (tmp_path / "generator-context.txt").write_bytes(data)
    output = tmp_path / "output"
    output.mkdir()
    (output / "feedback.json").write_text('{"closed":true}\n')
    admitted = generator_context(tmp_path, output)
    assert "OPENCODE_ACTION_FUSION" in admitted and 'JSON.stringify(\\"0\\")' in admitted
    assert "PRIVATE_QUALIFICATION_SENTINEL" not in admitted
    assert value["revision_recipe"]["dependencies"]["builder.ts"]["sha256"] in admitted
    assert sha256(encode(value["revision_recipe"])) in admitted
    assert '"closed":true' in admitted
    publication = json.loads(data.split(b"Frozen build context:\n", 1)[1])
    assert publication["inputs"][0]["source"] == (tmp_path / "builder.ts").read_text()
    assert generator_context_input(value) == data


def test_opt_out_preserves_old_context_bytes(tmp_path):
    value = study(tmp_path)
    del value["generator"]["build_context_dependencies"]
    assert generator_context_input(value) == (tmp_path / "context.txt").read_bytes()


@pytest.mark.parametrize(("names",), [([],), (["missing.ts"],), (["builder.ts", "builder.ts"],), ("builder.ts",), ([3],)])
def test_invalid_dependency_selection_is_rejected(tmp_path, names):
    value = study(tmp_path)
    value["generator"]["build_context_dependencies"] = names
    with pytest.raises(GrowthError, match="build context"):
        generator_context_input(value)


def test_mutated_build_input_cannot_silently_change_context(tmp_path):
    value = study(tmp_path)
    (tmp_path / "builder.ts").write_text("replacement")
    with pytest.raises(GrowthError, match="changed"):
        generator_context_input(value)


@pytest.mark.parametrize(("data",), [(b"bad\0source",), (b"\xff",), (b"x" * 65537,)], ids=["nul", "non-utf8", "oversized"])
def test_non_source_or_oversized_dependency_is_rejected(tmp_path, data):
    value = study(tmp_path)
    (tmp_path / "builder.ts").write_bytes(data)
    value["revision_recipe"]["dependencies"]["builder.ts"]["sha256"] = sha256(data)
    with pytest.raises(GrowthError):
        generator_context_input(value)
