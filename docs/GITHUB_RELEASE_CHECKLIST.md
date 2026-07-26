# GitHub Release Checklist

Use this checklist from the repository root before publishing.

## 1. Run Local Checks

```bash
./scripts/check_all.sh
```

Expected result:

```text
All checks passed.
```

## 2. Review Files That Would Be Committed

If this directory is not a git repository yet:

```bash
git init
git branch -M main
```

Then inspect the candidate file list without committing:

```bash
git add --dry-run .
```

Look for anything that must not be public:

```bash
git add --dry-run . | grep -E '(\.env\.local|\.venv|\.tools|\.tar\.gz|POTCAR|WAVECAR|CHGCAR|OUTCAR|vasp-env|/home/|Users/)'
```

That command should print nothing important. If it does, update `.gitignore` before committing.

## 3. Confirm The License

The repository ships with an MIT `LICENSE` file. Confirm the copyright holder
line is correct before making the repository public, and keep the license
mentioned in `README.md`.

## 4. Configure Git Identity

Commits permanently record the configured author. Set an identity you are
comfortable publishing (a GitHub noreply address avoids exposing a personal
email):

```bash
git config user.name "<display-name>"
git config user.email "<id>+<username>@users.noreply.github.com"
```

## 5. Commit

```bash
git status --short
git add .
git status --short
git commit -m "Prepare VASP Input Studio for public release"
```

## 6. Push

Create an empty GitHub repository, then:

```bash
git remote add origin git@github.com:<owner>/vasp-input-studio.git
git push -u origin main
```

## 7. Optional Release Archive

```bash
./packaging/build_linux_release.sh
```

Upload the generated `dist/*.tar.gz` file as a GitHub Release asset, not as a committed source file.
