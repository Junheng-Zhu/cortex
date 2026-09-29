from pathlib import Path
import subprocess

from cortex.runtime.checkpoint import InMemoryCheckpointStore
from cortex.runtime.execution_checkpoint import WorkspaceRecoveryRuntime
from cortex.runtime.state import AgentState
from cortex.runtime.workspace import ShadowGitSnapshotStore, WorkspaceOperation


def store(tmp_path: Path) -> ShadowGitSnapshotStore:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return ShadowGitSnapshotStore(workspace, tmp_path / "shadow")


def test_snapshot_diff_and_restore_create_modify_delete(tmp_path):
    snapshots = store(tmp_path)
    root = snapshots.workspace
    (root / "modified").write_text("A")
    (root / "deleted").write_text("keep me")
    before = snapshots.snapshot()
    (root / "modified").write_text("B")
    (root / "deleted").unlink()
    (root / "created").write_text("new")
    after = snapshots.snapshot()

    assert {x.path: x.operation for x in snapshots.diff(before, after)} == {
        "created": WorkspaceOperation.CREATED,
        "deleted": WorkspaceOperation.DELETED,
        "modified": WorkspaceOperation.MODIFIED,
    }
    snapshots.restore(before)
    assert (root / "modified").read_text() == "A"
    assert (root / "deleted").read_text() == "keep me"
    assert not (root / "created").exists()


def test_dirty_git_is_unchanged_and_cortex_is_excluded(tmp_path):
    snapshots = store(tmp_path)
    root = snapshots.workspace
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "a@b.c"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "A"], check=True)
    (root / "tracked").write_text("head")
    subprocess.run(["git", "-C", str(root), "add", "tracked"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "initial"], check=True)
    (root / "tracked").write_text("dirty")
    (root / ".cortex").mkdir()
    (root / ".cortex" / "runtime").write_text("do not snapshot")
    head = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"])
    index = (root / ".git" / "index").read_bytes()

    point = snapshots.snapshot()
    (root / "tracked").write_text("agent")
    snapshots.restore(point)

    assert (root / "tracked").read_text() == "dirty"
    assert (root / ".cortex" / "runtime").read_text() == "do not snapshot"
    assert subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"]) == head
    assert (root / ".git" / "index").read_bytes() == index


def test_execution_rollback_restores_logical_and_physical_state(tmp_path):
    snapshots = store(tmp_path)
    (snapshots.workspace / "file").write_text("before")
    agents = InMemoryCheckpointStore()
    runtime = WorkspaceRecoveryRuntime(snapshots, agents)
    state = AgentState(session_id="session", run_id="run", current_goal="before")
    target = runtime.checkpoint(state)
    state.current_goal = "after"
    runtime.mutate(state, lambda: (snapshots.workspace / "file").write_text("after"),
                   action_ids=("action",), wave_id="wave")

    restored = runtime.rollback(target.execution_checkpoint_id)

    assert restored.current_goal == "before"
    assert (snapshots.workspace / "file").read_text() == "before"
    assert runtime.ledger.records[0].action_id == "action"
    assert runtime.execution_checkpoints.latest("session").reason == "post_rollback"
