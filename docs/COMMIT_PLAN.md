# Commit plan (do these as you go, so the history is real)

Create an empty public repo on GitHub, then in the project folder:

```bash
git init && git branch -M main
git remote add origin https://github.com/<you>/<repo>.git
```
Commit in stages, running the app or tests between steps. Suggested messages:

1. `git add db.py requirements.txt .gitignore && git commit -m "Add database schema and connection layer"`
2. `git add engine.py && git commit -m "Add ledger-based stock engine, units and BOM production logic"`
3. `git add seed.py benchmark.py && git commit -m "Add section 5 seed data and benchmark scenario"`
4. `git add tests && git commit -m "Add tests for stock, tax, units, audit and benchmark"`
5. `git add reports.py exports.py && git commit -m "Add reports with CSV, Excel and PDF export and GST invoice PDF"`
6. `git add app.py templates static && git commit -m "Add role-based web app, dark theme and mobile order entry"`
7. `git add README.md docs && git commit -m "Add README, design note and code guide"`
8. `git push -u origin main`

Then make a few small real commits of your own (fix a label, tweak a colour, add the demo video link to the README).
