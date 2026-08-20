# Nested Bun Toolchain Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve a verified Bun directory in the strict installer subprocess PATH so Bun postinstall subprocesses can execute without inheriting the parent environment.

**Architecture:** `campaign_process` resolves commands from the caller PATH and launches them with only a verified executable directory plus `os.defpath`. `campaign_install` freezes Bun identity in built-arm manifests, revalidates it before install/build, and rejects unsafe or changed tooling before publishing an install.

**Tech Stack:** Python 3.10+, pytest, subprocess, SHA-256, cgroup-v2 containment.

---

### Task 1: Reproduce the nested executable failure

**Files:**
- Modify: `tests/test_run_opencode.py`
- Test: `tests/test_run_opencode.py`

- [ ] **Step 1: Write a fake parent-PATH-only Bun that runs `bun nested-ok` from its install action and prints its nested executable and environment.**
- [ ] **Step 2: Run `uv run --frozen pytest tests/test_run_opencode.py -k nested_bun -q` and verify it fails because the child PATH contains only `os.defpath`.**

### Task 2: Construct a safe command environment

**Files:**
- Modify: `scripts/campaign_process.py`
- Test: `tests/test_run_opencode.py`

- [ ] **Step 1: Resolve command zero against the explicit/current parent PATH, reject unsafe names, paths, symlink chains, nonregular files, and nonexecutable files.**
- [ ] **Step 2: Launch with the resolved absolute executable and a PATH consisting of its trusted real directory followed by unique `os.defpath` directories.**
- [ ] **Step 3: Run the nested Bun test and verify it passes, captures the exact Bun path, and excludes a fake API key.**

### Task 3: Freeze and verify built-arm tool identity

**Files:**
- Modify: `scripts/campaign_install.py`
- Modify: `tests/test_run_opencode.py`

- [ ] **Step 1: Add failing tests for strict built-arm manifest toolchain fields, missing Bun before worktree creation, and changed Bun after identity freeze.**
- [ ] **Step 2: Freeze realpath, hash, version, and sanitized PATH directories in `InstallRecord`; validate on manifest reuse and immediately before install/build.**
- [ ] **Step 3: Keep supplied-binary records explicitly toolchain-empty and reject malformed/unsafe manifest identities.**
- [ ] **Step 4: Run focused installer tests, then `uv run --frozen pytest`, Ruff, and a clean diff check.**

### Task 4: Commit the verified fix

**Files:**
- Modify: `scripts/campaign_process.py`
- Modify: `scripts/campaign_install.py`
- Modify: `tests/test_run_opencode.py`

- [ ] **Step 1: Re-read the requirement checklist against the diff and test evidence.**
- [ ] **Step 2: Commit with `fix(campaign): preserve nested Bun toolchain`.**
