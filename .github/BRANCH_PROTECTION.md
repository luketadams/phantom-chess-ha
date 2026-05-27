# Branch protection setup (manual)

The CI workflow `.github/workflows/test.yml` runs three required jobs
(`matrix-tests (3.12)`, `matrix-tests (3.13)`, `lint`) plus an
informational `ha-tests` job. Wiring them as required status checks
on `main` is currently a manual step because the fine-grained PAT that
pushes to this repo does not carry the "Administration: write"
permission required by the
[Update branch protection REST endpoint](https://docs.github.com/en/rest/branches/branch-protection#update-branch-protection).

## Option A — GitHub web UI (fastest)

1. Go to <https://github.com/luketadams/phantom-chess-ha/settings/branches>.
2. Click **Add branch protection rule**.
3. Branch name pattern: `main`.
4. Tick **Require status checks to pass before merging**.
   - Add: `matrix-tests (3.12)`, `matrix-tests (3.13)`, `lint`.
   - Leave **Require branches to be up to date before merging** off (would
     force frequent rebases during the alpha-iteration cadence).
5. Leave **Require a pull request before merging** off — this is a
   solo-dev repo and the cadence is direct-to-main.
6. Tick **Do not allow bypassing the above settings** off — admins should
   keep the escape hatch.
7. **Restrict deletions** and **block force pushes** under "Rules applied
   to everyone" — these are the most valuable solo-dev guardrails.
8. Save.

## Option B — `gh` CLI after scope grant

Grant the `phantom-chess-ha` fine-grained PAT "Administration: read and
write" at
<https://github.com/settings/personal-access-tokens>. Then:

```bash
gh api -X PUT \
  -H "Accept: application/vnd.github+json" \
  /repos/luketadams/phantom-chess-ha/branches/main/protection \
  --input .github/branch_protection.json
```

A ready-to-POST body lives at `.github/branch_protection.json` (see
file).

## Why this is a `.github/` doc and not memory

Solo-dev branch protection is a one-time setup that lives next to the
CI config it gates. Putting it here keeps the runbook discoverable
without polluting the persistent memory store.
