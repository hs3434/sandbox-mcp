# Contributing to sandbox-mcp

Thank you for your interest in contributing! sandbox-mcp is **dual
licensed** (AGPLv3 + commercial), so before you open a pull request,
please read the licence-related sections below — they take only a few
minutes and protect both you and the project.

## 1. Contribution license & sign-off

By submitting a contribution (pull request, patch, attached file,
mailing-list post, etc.), you agree to the following:

- **Your contribution is your own work**, or you have the necessary
  rights to submit it. If your contribution includes third-party
  material, you have already identified the source and confirmed
  the licence is compatible.
- **You licence it under AGPLv3**, the same licence as the rest of
  the project (see [`LICENSE`](LICENSE)).
- **You grant the maintainer a perpetual right to relicense** your
  contribution under the project's commercial terms
  (see the README **License** section) — this is what makes the
  dual-licensing model work, and is the only authority that lets the
  maintainer relicense your changes to a third-party commercial
  customer.

### How you actually sign

For casual contributions the project uses the **Developer Certificate
of Origin 1.1** style sign-off — add the line below to **every
commit message** (use `git commit -s` to do it automatically):

```
Signed-off-by: Your Full Name <you@example.com>
```

By including that line you certify, on the record, the three points
above. The "Signed-off-by" matches your `git config user.name` /
`user.email` and acts as your signature; no paper forms required.

> **Tip:** configure Git once with
> `git config user.name "Your Name"` and
> `git config user.email "you@example.com"` so the `-s` flag always
> fills the right values.

If you add a commit later (e.g., during review) and forget the
sign-off, fix it with:

```bash
git commit --amend --no-edit -s   # amend the latest commit
# or, for older commits:
git rebase --exec 'git commit --amend --no-edit -s' HEAD~N
```

For **substantial / recurring contributors** (or where your
employer's IP policy demands a paper trail), send a signed
Contributor License Agreement to **[1606272735@qq.com](mailto:1606272735@qq.com)**;
the maintainer will countersign and return a copy. A corporate version
is available on request.

## 2. Code of conduct

The project follows the spirit of a typical open-source Code of
Conduct: be respectful, focus on technical merit, and presume good
faith. Constructive disagreement is welcome; personal attacks are not.

## 3. Development workflow

1. **Fork & branch.** Fork the repository and create a topic branch
   off `main`: `git checkout -b my-feature`.
2. **Install dev deps.**
   ```bash
   pip install -e ".[dev]"
   ```
3. **Make your changes.** Follow the existing code style and
   conventions — `ruff` (line length 100) and `mypy` are the source
   of truth.
4. **Run the local CI** mirror before pushing:
   ```bash
   ./scripts/ci.sh
   ```
   This runs `ruff format --check`, `ruff check`, `mypy src/sandbox_mcp`,
   and the unit tests — exactly the steps GitHub Actions runs.
5. **Sign off your commits.** `git commit -s` for every commit.
   Pull requests whose history lacks `Signed-off-by` will not pass
   the project's DCO check (recommended to enable as a GitHub Action
   on this repo).
6. **Open a pull request.** Describe the motivation and the design;
   link any related issues. Small, focused PRs are easier to review
   and merge than sprawling ones.
7. **Address review feedback.** Push additional commits to the same
   branch; the CI re-runs automatically.

### Testing

- **Unit tests** run on every push and PR:
  ```bash
  pytest tests/ -v
  ```
- **Integration tests** (require a running Docker daemon) are opt-in:
  ```bash
  pytest tests/ -m integration -v
  ```

### Commit messages

- Use the imperative mood ("Add foo", not "Added foo").
- The first line should be ≤ 72 characters and summarise the change.
- Reference issue numbers with `#NNN` where applicable.
- Always include the `Signed-off-by` line (see § 1).

### Style

- Line length: 100 (enforced by `ruff`).
- Python ≥ 3.12 syntax (the project targets `>=3.12` in `pyproject.toml`).
- Prefer composition over inheritance; prefer `pathlib.Path` over
  string concatenation; prefer explicit imports over star-imports.
- Type hints on all new public functions; `mypy` is configured with
  `disallow_untyped_defs = false` today, but new code should be typed.

## 4. Filing issues

- Search the existing issues first.
- Use the issue templates when present; otherwise include:
  - `sandbox-mcp` version (`pip show sandbox-mcp`),
  - transport (`stdio` / `streamable-http`),
  - OS, Docker daemon version (if relevant), Python version,
  - minimal reproduction steps and observed vs expected behaviour.
- For **security** issues, **do not** open a public issue — email
  **[1606272735@qq.com](mailto:1606272735@qq.com)** instead.

## 5. Licensing recap

| Scenario | License that applies |
|----------|----------------------|
| You are an end user running the software for yourself | AGPLv3 (free) |
| You are a SaaS / hosted-service provider without paying for a commercial licence | AGPLv3 — you **must** publish the source of any modifications you serve |
| You want to integrate sandbox-mcp into a proprietary product and keep your modifications closed | You need a **Commercial License**; contact **1606272735@qq.com** |
| You are contributing patches back to the upstream project | AGPLv3 + your contribution is also relicensable under the Commercial License per § 1 above |

Thanks for contributing — and for helping keep the project's dual
licensing model sustainable!
