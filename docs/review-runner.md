# Windows review runner

The owner applies these host changes. A repository change does not update an
already running runner. Do not run this installer against the live runner or
restart it as part of an issue implementation.

The review runner uses the owner-account persistent setup described under
**Owner-account persistent runner**. The stricter setup, a dedicated standard
account with one job per registration, is documented under **Stricter option**.

## Controls

- **Interaction limits.** The repository's interaction limit is set to
  **collaborators only** and expires on **2027-04-12**. The same limit applies
  to agent-harness. Outside accounts cannot open issues, pull requests, or
  comments, so no text from outside the repository reaches the reviewer. A limit
  lasts six months at most. When it expires, the restriction ends, so the owner
  must renew it before that date.
- In GitHub **Settings → Actions → General → Fork pull request workflows**, keep
  **Require approval for all external contributors** selected. The API reports
  this as `all_external_contributors`.
- `.github/workflows/review.yml` uses `pull_request_target` and
  `workflow_dispatch`; its job guard skips fork heads and Dependabot PRs. The
  shared reviewer also checks the PR before using its head as review context.
- `ops/github/review-job-started.ps1` is a host-side allow-list. It requires
  `dflippojr/agent-harness-hub/.github/workflows/review.yml@refs/heads/main`,
  `GITHUB_REF=refs/heads/main`, and either `pull_request_target` or
  `workflow_dispatch`. It also checks the event's repository and default branch;
  PR events must have a base on main and both base and head in this repository.
  Missing or malformed context and every other job fail before workflow steps.
  Reusable workflow jobs retain the caller's workflow context; the allow-list
  names this repository's caller, not the shared Agent Harness implementation.

The hook is defense in depth, not a sandbox. Labels route jobs; they do not
provide authorization. The reviewer reads untrusted PR text with shell access.
If the default branch changes, the owner must update the hook's explicit `main`
allow-list before provisioning replacements.

Each repository has its own hook and runner. Do not register this runner with
another repository's installer (or Agent Harness's `install-runner.ps1`, which
installs no hook), and do not widen another repository's hook to cover this one:
a runner without this hook in its `.env` runs any job that requests its label.

## Residual risk

The owner-account persistent runner accepts that a review job can reach what
the owner's Windows account can reach: its profile, files, and credentials.
The runner is not ephemeral, so its state and work directory persist from one
job to the next, and a job that changes them can affect later jobs. The
interaction limit and fork approval reduce who can supply untrusted text. They
do not make a permitted review safe. The owner accepts this risk for
`dflippojr/agent-harness-hub`. The interaction limit must be renewed before
2027-04-12, or the review runner is again exposed to text from outside accounts.

## Owner-account persistent runner

This is the default setup. The current runner is registered as
`dflippotower-agent-harness-hub-review` with label `agent-harness-hub-review`,
installed in `D:\Agents\agent-harness-hub-review`. It runs from the logon task
`AgentHarnessHub-GitHubRunner-Review` under the owner's own Windows account and
is **not** ephemeral.

1. Sign in as the owner's account and open a non-elevated PowerShell. Never run
   the installer elevated: it refuses an elevated token. It also refuses a
   member of Administrators unless `-OwnerAccount` is passed, so add that switch
   only when the owner's account is in Administrators.
2. Copy the scripts from the trusted default branch. Use a new, empty runner
   directory and a separate hook directory outside the checkout. The installer
   refuses a runner directory that is not empty, including one with a `.runner`
   file from an earlier registration. It verifies the pinned runner archive's
   SHA-256.
3. Obtain a fresh repository registration token through an authorized operator.
   Supply it in memory to the account running the installer; never save it in
   source, shell history, task arguments, or logs. For example, where that account
   has the required GitHub permission:

   ```powershell
   $token = gh api -X POST repos/dflippojr/agent-harness-hub/actions/runners/registration-token --jq .token
   .\ops\github\install-runner.ps1 -Token $token -Persistent
   Remove-Variable token
   ```

   `-Persistent` leaves out `--ephemeral`, so the runner stays registered and
   takes each review job as it arrives. Add `-OwnerAccount` to that command only
   when the owner's account is in Administrators.

   Defaults are `D:\Agents\agent-harness-hub-review`, runner name and label
   `agent-harness-hub-review`, and task `AgentHarnessHub-GitHubRunner-Review`.
   The installer copies the hook to `D:\Agents\agent-harness-hub-review-hooks`
   and writes the runner's `.env` with one line: `ACTIONS_RUNNER_HOOK_JOB_STARTED`
   set to the hook's absolute path. Custom `-InstallDir`, `-HookDir`, `-Name`,
   and `-TaskName` allow separate pool members; each `-InstallDir` must be new
   or empty.
4. The hidden task starts immediately and at logon under this same account,
   with `RunLevel Limited` and an interactive logon. It does not install a
   system service or run as SYSTEM. The account must remain signed in.
5. Restrict the runner and hook directories' ACLs to the owner's account and
   operators. Other local users must not be able to replace the hook.

Do not copy other private data into the runner's directories. The runner keeps
its registration and work directory under the owner's account, so keep the
review tools and credentials it needs to the minimum.

## Stricter option: dedicated account and one job

Use this where the review runner must be kept apart from the owner's own
account. It is the installer's default when `-Persistent` and `-OwnerAccount`
are omitted.

1. Create a dedicated **standard (non-administrator)** Windows account for
   reviews. Sign in as that account and provision only the review tools and
   credentials it needs. Do not copy the owner's profile or other private data.
   Restrict the runner and hook directories' ACLs to this account and operators;
   other local users must not be able to replace the hook.
2. Follow the steps above with the same scripts, directories and token rules,
   but without `-Persistent` or `-OwnerAccount`. The installer refuses an
   elevated token and any member of Administrators, so the account must be a
   standard account. The installer then registers with `--ephemeral`.
3. The hidden task starts immediately and at logon under this account, with
   `RunLevel Limited` and an interactive logon, as above.

Registration uses `--ephemeral`: GitHub assigns one job, then deregisters the
runner. The logon task cannot register it again and no token is stored for
automatic re-registration. An owner must provision another runner before the
next review; queued reviews wait for an available registration.

After each job, preserve `_diag` logs in operator-controlled storage outside
the disposable installation, confirm the runner process has exited, and remove
its old scheduled task. Clean the work directory and reprovision from trusted
scripts and a verified archive in a fresh directory with a fresh token. Keep
review credentials narrowly scoped. Ephemeral registration does **not** erase
the Windows profile, workspace, credentials, or logs and does not itself provide
a fresh host. A disposable host gives stronger isolation where available.

For an existing persistent runner, the owner must arrange its retirement and
replacement separately. This installer deliberately does not migrate or
re-register that runner in place.

## Verification

Run `python -m pytest tests/test_review_runner.py -q` with PowerShell available
(`pwsh`, or `powershell.exe` on Windows). The tests exercise accepted and rejected
synthetic event contexts and mock download, registration, and scheduled-task
operations; they never change the live runner. These script tests skip on hosts
without PowerShell. The administrator-account refusal test runs only on a
non-elevated account that is a member of Administrators; elsewhere it is skipped.
Verify the deployed `.env`, hook ACLs, limited task identity, the registration's
ephemeral or persistent mode, and the **Set up runner** hook log when the owner
applies the change.

References: [GitHub job hooks](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/run-scripts)
and [ephemeral registration](https://docs.github.com/en/actions/reference/runners/self-hosted-runners#ephemeral-runners-for-autoscaling).
