from pathlib import Path


def test_runtime_uses_the_cortex_package_layout():
    root = Path(__file__).resolve().parents[1]
    expected = {
        "app/cli.py",
        "app/bootstrap.py",
        "runtime/loop.py",
        "runtime/state.py",
        "llm/client.py",
        "llm/protocol.py",
        "tools/executor.py",
        "tools/builtin/artifacts.py",
        "context/manager.py",
        "context/artifacts.py",
        "memory/manager.py",
        "skills/registry.py",
        "orchestration/router.py",
        "observability/tracer.py",
    }

    assert all((root / "cortex" / path).is_file() for path in expected)
    assert all(not (root / legacy).exists() for legacy in ("agent", "runtime", "src", "playground"))
