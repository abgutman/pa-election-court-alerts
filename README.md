# Pennsylvania election-court alerts

This public-record monitor searches for election litigation affecting the Philadelphia region, Pennsylvania's 1st and 8th congressional districts, and Pennsylvania appellate courts. It records source health, de-duplicates results, writes JSON/HTML output, and can email the configured reporting inbox.

The workflow is configured to poll every 10 minutes and can also be run manually.

## Geographic and court scope

The county scope is Philadelphia; the four collar counties (Bucks, Chester, Delaware, and Montgomery); and the PA-08 counties (Lackawanna, Luzerne, Monroe, Pike, and Wayne). PA-01 consists of Bucks and part of Montgomery. PA-08 consists of Lackawanna and Wayne, plus parts of Luzerne, Monroe, and Pike.

The monitored court inventory is:

| Court | Source | Automated status |
|---|---|---|
| Pennsylvania Commonwealth, Supreme, and Superior Courts | UJS appellate search; official opinion RSS | Active; matching Commonwealth dockets and opinions are high priority |
| U.S. District Court, Eastern District of Pennsylvania | official CM/ECF RSS; CourtListener RECAP | Active |
| U.S. District Court, Middle District of Pennsylvania | official CM/ECF RSS; CourtListener RECAP | Active |
| U.S. Court of Appeals for the Third Circuit | CourtListener RECAP | Active |
| Philadelphia Common Pleas | FJD data through the existing `av-tools` mirror | Active, but upstream publication is periodic |
| Bucks and Montgomery Common Pleas | PSI Web Viewer | Active when the viewer is not CAPTCHA-gated |
| Delaware Common Pleas | C-Track Public Access | Active |
| Monroe Common Pleas | C-Track Public Access | Active when the county endpoint is available |
| Chester Common Pleas | ChescoPIN | Scoped; paid account needed for remote docket access |
| Lackawanna and Wayne Common Pleas | INFOCON | Scoped; no dependable anonymous bulk feed |
| Luzerne Common Pleas | SearchIQS | Scoped; no dependable anonymous bulk feed |
| Pike Common Pleas | CountySuite/Prothonotary | Scoped; no dependable anonymous bulk filing feed |

"10 minutes" describes how often GitHub Actions will poll after activation. It does not make a court publish within ten minutes, and GitHub's scheduled jobs can be delayed. Every report therefore distinguishes an empty result from a failed, delayed, or unavailable source.

For Commonwealth Court, the monitor text-checks newly opened docket sheets and newly published opinion PDFs. Once a new Commonwealth docket matches the election watch list, its official docket sheet remains tracked so later changes generate high-priority alerts. The anonymous UJS source identifies that the docket changed but may not expose a precise posting time for each entry.

## Watch list

[`config/watchlist.json`](config/watchlist.json) contains:

- the members and directors of every county election board in scope, plus the Pennsylvania chief election official;
- the election boards themselves;
- the PA-01 and PA-08 general-election candidates and their principal campaign committees;
- state, national, and county political parties;
- PACs and outside groups found in current FEC independent-expenditure records for PA-01 and PA-08; and
- generic election-litigation phrases such as `election`, `ballot`, `voter`, `recount`, `polling place`, and `nomination petition`.

The FEC list is intentionally broader than organizations with `PAC` in their names because Schedule E also contains independent-expenditure filers and politically active organizations.

## Local use

Secrets stay in `~/.zprofile`; the repository contains no credentials.

```bash
source ~/.zprofile
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python tracker.py --since '2026-11-02T08:00:00-05:00' --test --replay --send
```

Email uses `GMAIL_USER` and `GMAIL_APP_PASSWORD`. Delivery requires an explicit `ALERT_TO`; the monitor does not fall back to the sender address. CourtListener uses `CL_TOKEN` when available.

The run writes `output/latest.json`, `output/latest.html`, and hash/ID-only deduplication state in `data/state.json`. County sources that expose only dates cannot distinguish 7:59 a.m. from 8:01 a.m.; the catch-up test therefore conservatively includes every matching Nov. 2 date-only filing.

Run tests with:

```bash
python -m unittest discover -s tests -v
```

## GitHub setup

Create a public repository, push this tree, then load the four Actions secrets from the already exported shell variables:

```bash
source ~/.zprofile
python scripts/configure_github_secrets.py OWNER/REPOSITORY
```

The workflow supports both `workflow_dispatch` and a `*/10 * * * *` schedule. It has `contents: write` permission so each successful or partially successful run can persist deduplication state and the latest source-health report.

## Source references

- [Pennsylvania congressional district descriptions](https://www.pa.gov/agencies/vote/resources/redistricting/pennsylvania-redistricting-us-congress)
- [Pennsylvania county election officials](https://www.pa.gov/agencies/vote/contact-us/contact-your-election-officials)
- [Pennsylvania UJS case information](https://www.pacourts.us/public-records/court-case-information)
- [Monroe County C-Track description](https://www.monroecountypa.gov/departments/prothonotary/resources/c-tracksearch)
- [Chester County Prothonotary / ChescoPIN](https://www.chesco.org/Prothonotary)
- [Luzerne County Prothonotary / SearchIQS](https://www.luzernecounty.org/594/Prothonotary)
- [Wayne County Prothonotary / INFOCON](https://www.waynecountypa.gov/458/Prothonotary)
- [Federal Election Commission API](https://api.open.fec.gov/developers/)

This is a reporting aid, not a complete court docket or legal advice. Confirm consequential records with the filing court.
