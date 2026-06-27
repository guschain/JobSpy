# Job Finger Workspace

This folder is the user workspace. In normal use, only touch this folder.

Put your input here:

- `cv.pdf`: your CV/resume PDF. This is the main file you add manually.
- `config.json`: profile, target jobs, search defaults, and preferences.
- `observation_template.md`: notes template used in the UI.
- `cover_letter_template.md`: short guidance for generated application drafts.

The app writes output here:

- `data/jobs.jsonl`: latest deduped ranked jobs.
- `data/scrapes.jsonl`: append-only scrape history.
- `data/applications.jsonl`: applications, notes, and status events.
- `data/feedback.jsonl`: negative keywords learned from bad recommendations.
- `cv.md`: text extracted from `cv.pdf` by MarkItDown.
- `cv_profile.json`: structured CV signals and evidence snippets generated from `cv.md`.
- `briefs/`: application prep briefs.
- `cover_letters/`: standalone cover-letter drafts.
- `exports/`: optional CSV exports.

Start from the repo root:

```powershell
.\update-cv.ps1
.\search-jobs.ps1
.\start-ui.ps1
```

All three scripts run through `uv`. `.\update-cv.ps1` also re-scores existing
stored jobs when `data/jobs.jsonl` exists. If the UI is already running,
`.\start-ui.ps1` prints the existing local URL instead of starting a duplicate.

Useful local filters:

```powershell
uv run job-finger rank --work-mode hybrid --seniority senior --min-salary 40000
uv run job-finger rank --exclude-keyword sap --exclude-scope content
uv run job-finger rank --min-cv-matches 2 --max-cv-gaps 3 --no-negative
uv run job-finger rank --sort salary
```

The UI Match tab shows detected job skills, CV matches, CV evidence snippets,
likely gaps, application suggestions, and a cover-letter draft for each stored
job. Use `Save Brief` there to write a Markdown brief into `workspace/briefs/`
and a standalone cover letter into `workspace/cover_letters/`.
