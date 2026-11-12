# ChatGPT / GitHub / DeepSeek integration test repository

This private repository contains synthetic test data only. It is an explicitly authorized integration sandbox, not a production project.

The current fixture verifies **GitHub connectivity only**:

1. An Issue receives `integration:probe` from the repository owner.
2. A GitHub-hosted Ubuntu Actions job validates the event and writes `GITHUB_LINK_PROBE_RECEIPT` back to that Issue, including its real Actions run URL.
3. A separate pull-request workflow runs actual Python unit tests against the PR revision.

Issue body text is never executed. The receipt is a fixed-purpose metadata message. Neither workflow invokes DeepSeek, registers a Windows self-hosted Runner, creates a PR automatically, or impersonates a ChatGPT web Scheduled Task.

**A green run proves only the corresponding GitHub workflow passed. It does not prove the full DeepSeek / Windows / ChatGPT unattended loop works.** Those components must be separately commissioned and verified with their own evidence.

Run the same fixture tests locally with:

```console
python -m unittest discover -s tests -v
```

Only `.github/workflows/issue-probe.yml` grants `issues: write`, solely to post the probe receipt. Its script checks the exact repository, trusted event sender, label, open Issue number and run ID. It sends one POST with a 20-second timeout and never blindly retries an uncertain write.
