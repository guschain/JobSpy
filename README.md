<img src="https://github.com/cullenwatson/JobSpy/assets/78247585/ae185b7e-e444-4712-8bb9-fa97f53e896b" width="400">

**JobSpy** is a job scraping library with the goal of aggregating all the jobs from popular job boards with one tool.

## Features

- Scrapes job postings from **LinkedIn**, **Indeed**, **Glassdoor**, **Google**, **ZipRecruiter**, & other job boards concurrently
- Aggregates the job postings in a dataframe
- Proxies support to bypass blocking

![jobspy](https://github.com/cullenwatson/JobSpy/assets/78247585/ec7ef355-05f6-4fd3-8161-a817e31c5c57)

### Installation

```
pip install -U python-jobspy
```

_Python version >= [3.10](https://www.python.org/downloads/release/python-3100/) required_

### Usage

```python
import csv
from jobspy import scrape_jobs

jobs = scrape_jobs(
    site_name=["indeed", "linkedin", "zip_recruiter", "google"], # "glassdoor", "bayt", "naukri", "bdjobs"
    search_term="software engineer",
    google_search_term="software engineer jobs near San Francisco, CA since yesterday",
    location="San Francisco, CA",
    results_wanted=20,
    hours_old=72,
    country_indeed='USA',
    
    # linkedin_fetch_description=True # gets more info such as description, direct job url (slower)
    # proxies=["208.195.175.46:65095", "208.195.175.45:65095", "localhost"],
)
print(f"Found {len(jobs)} jobs")
print(jobs.head())
jobs.to_csv("jobs.csv", quoting=csv.QUOTE_NONNUMERIC, escapechar="\\", index=False) # to_excel
```

### Output

```
SITE           TITLE                             COMPANY           CITY          STATE  JOB_TYPE  INTERVAL  MIN_AMOUNT  MAX_AMOUNT  JOB_URL                                            DESCRIPTION
indeed         Software Engineer                 AMERICAN SYSTEMS  Arlington     VA     None      yearly    200000      150000      https://www.indeed.com/viewjob?jk=5e409e577046...  THIS POSITION COMES WITH A 10K SIGNING BONUS!...
indeed         Senior Software Engineer          TherapyNotes.com  Philadelphia  PA     fulltime  yearly    135000      110000      https://www.indeed.com/viewjob?jk=da39574a40cb...  About Us TherapyNotes is the national leader i...
linkedin       Software Engineer - Early Career  Lockheed Martin   Sunnyvale     CA     fulltime  yearly    None        None        https://www.linkedin.com/jobs/view/3693012711      Description:By bringing together people that u...
linkedin       Full-Stack Software Engineer      Rain              New York      NY     fulltime  yearly    None        None        https://www.linkedin.com/jobs/view/3696158877      Rain’s mission is to create the fastest and ea...
zip_recruiter Software Engineer - New Grad       ZipRecruiter      Santa Monica  CA     fulltime  yearly    130000      150000      https://www.ziprecruiter.com/jobs/ziprecruiter...  We offer a hybrid work environment. Most US-ba...
zip_recruiter Software Developer                 TEKsystems        Phoenix       AZ     fulltime  hourly    65          75          https://www.ziprecruiter.com/jobs/teksystems-0...  Top Skills' Details• 6 years of Java developme...

```

### Parameters for `scrape_jobs()`

```plaintext
Optional
├── site_name (list|str): 
|    linkedin, zip_recruiter, indeed, glassdoor, google, bayt, bdjobs
|    (default is all)
│
├── search_term (str)
|
├── google_search_term (str)
|     search term for google jobs. This is the only param for filtering google jobs.
│
├── location (str)
│
├── distance (int): 
|    in miles, default 50
│
├── job_type (str): 
|    fulltime, parttime, internship, contract
│
├── proxies (list): 
|    in format ['user:pass@host:port', 'localhost']
|    each job board scraper will round robin through the proxies
|
├── is_remote (bool)
│
├── results_wanted (int): 
|    number of job results to retrieve for each site specified in 'site_name'
│
├── easy_apply (bool): 
|    filters for jobs that are hosted on the job board site (LinkedIn easy apply filter no longer works)
|
├── user_agent (str): 
|    override the default user agent which may be outdated
│
├── description_format (str): 
|    markdown, html (Format type of the job descriptions. Default is markdown.)
│
├── offset (int): 
|    starts the search from an offset (e.g. 25 will start the search from the 25th result)
│
├── hours_old (int): 
|    filters jobs by the number of hours since the job was posted 
|    (ZipRecruiter and Glassdoor round up to next day.)
│
├── verbose (int) {0, 1, 2}: 
|    Controls the verbosity of the runtime printouts 
|    (0 prints only errors, 1 is errors+warnings, 2 is all logs. Default is 2.)

├── linkedin_fetch_description (bool): 
|    fetches full description and direct job url for LinkedIn (Increases requests by O(n))
│
├── linkedin_company_ids (list[int]): 
|    searches for linkedin jobs with specific company ids
|
├── country_indeed (str): 
|    filters the country on Indeed & Glassdoor (see below for correct spelling)
|
├── enforce_annual_salary (bool): 
|    converts wages to annual salary
|
├── ca_cert (str)
|    path to CA Certificate file for proxies
```

```
├── Indeed limitations:
|    Only one from this list can be used in a search:
|    - hours_old
|    - job_type & is_remote
|    - easy_apply
│
└── LinkedIn limitations:
|    Only one from this list can be used in a search:
|    - hours_old
|    - easy_apply
```

## Supported Countries for Job Searching

### **LinkedIn**

LinkedIn searches globally & uses only the `location` parameter. 

### **ZipRecruiter**

ZipRecruiter searches for jobs in **US/Canada** & uses only the `location` parameter.

### **Indeed / Glassdoor**

Indeed & Glassdoor supports most countries, but the `country_indeed` parameter is required. Additionally, use the `location`
parameter to narrow down the location, e.g. city & state if necessary. 

You can specify the following countries when searching on Indeed (use the exact name, * indicates support for Glassdoor):

|                      |              |            |                |
|----------------------|--------------|------------|----------------|
| Argentina            | Australia*   | Austria*   | Bahrain        |
| Belgium*             | Brazil*      | Canada*    | Chile          |
| China                | Colombia     | Costa Rica | Czech Republic |
| Denmark              | Ecuador      | Egypt      | Finland        |
| France*              | Germany*     | Greece     | Hong Kong*     |
| Hungary              | India*       | Indonesia  | Ireland*       |
| Israel               | Italy*       | Japan      | Kuwait         |
| Luxembourg           | Malaysia     | Mexico*    | Morocco        |
| Netherlands*         | New Zealand* | Nigeria    | Norway         |
| Oman                 | Pakistan     | Panama     | Peru           |
| Philippines          | Poland       | Portugal   | Qatar          |
| Romania              | Saudi Arabia | Singapore* | South Africa   |
| South Korea          | Spain*       | Sweden     | Switzerland*   |
| Taiwan               | Thailand     | Turkey     | Ukraine        |
| United Arab Emirates | UK*          | USA*       | Uruguay        |
| Venezuela            | Vietnam*     |            |                |

### **Bayt**

Bayt only uses the search_term parameter currently and searches internationally



## Notes
* Indeed is the best scraper currently with no rate limiting.  
* All the job board endpoints are capped at around 1000 jobs on a given search.  
* LinkedIn is the most restrictive and usually rate limits around the 10th page with one ip. Proxies are a must basically.

## Job Finger Portugal Layer

This fork adds a separate `job_finger` package on top of JobSpy. The upstream
scrapers are left intact; Portugal support for the broad boards is already
available through `country_indeed="Portugal"` and LinkedIn's `location`
parameter.

The added layer is focused on filtering before applying:

- Portugal-first search defaults for Indeed and LinkedIn
- explainable fit scoring by skills, title, seniority, location, remote setup,
  salary, language, and recency
- CV PDF ingestion through MarkItDown, converted to `workspace/cv.md` and used
  as extra matching keywords
- normalized job signals for salary, work mode, seniority, detected skills,
  CV matches, CV gaps, positive/negative keywords, and application suggestions
- simple file storage in one folder: scrape history, latest ranked jobs, and
  application events as JSONL
- status tracking for saved, applied, follow-up, interview, offer, rejected, and
  ignored jobs
- application briefs with resume emphasis and cover-letter angles

Create a local config:

```bash
uv run job-finger init
```

Put your CV at `workspace/cv.pdf`, convert it with MarkItDown, then edit the
generated profile in `workspace/config.json`:

```bash
uv run job-finger cv
uv run job-finger search --top 15
uv run job-finger rank --min-score 60
```

Run an ad hoc keyword search without editing the config:

```bash
uv run job-finger search --keyword python --keyword fastapi --location Portugal
uv run job-finger search --related-to backend --remote --results 25
uv run job-finger search --keywords python fastapi postgres --match all
```

Filter local data by exact or related terms:

```bash
uv run job-finger rank --keyword fastapi
uv run job-finger rank --related-to ai --min-score 55
uv run job-finger rank --published-from 2026-06-01 --published-to 2026-06-26
uv run job-finger rank --exclude-keyword sap --exclude-scope content
uv run job-finger rank --work-mode hybrid --seniority senior --min-salary 40000
uv run job-finger rank --min-cv-matches 2 --max-cv-gaps 3 --no-negative
uv run job-finger rank --sort newest
```

Related groups are configurable under `related_keyword_groups` in
`job_finger.config.json`. Defaults include backend, frontend, fullstack, data,
ai, devops, security, qa, product, and mobile.

The user-facing workspace is intentionally small:

```plaintext
workspace/
  cv.pdf                  <- put your CV here
  cv.md                   <- generated from the PDF
  cv_profile.json         <- structured CV signals generated from cv.md
  config.json             <- your profile and searches
  observation_template.md <- notes template for the UI
  cover_letter_template.md
  data/
    jobs.jsonl
    scrapes.jsonl
    applications.jsonl
```

CSV is created only when explicitly requested:

```bash
uv run job-finger rank --csv workspace/exports/jobs.csv
```

Track an application:

```bash
uv run job-finger track in-example --status applied --notes "Applied with backend CV"
```

Start the local UI:

```bash
uv run job-finger ui
```

The UI reads `workspace/data`, runs keyword searches, filters the local job
list, shows salary/work-mode/seniority/type/date when captured, and displays
application history. Each job detail has a Match tab with CV matches, likely
gaps, detected skills, application suggestions, and a deterministic cover-letter
draft. Use `Save Brief` in that tab to write a Markdown prep file into
`workspace/briefs/`. Customize UI observations in
`workspace/observation_template.md`.
The list view also includes summary counters and quick actions to save, ignore,
or generate a brief directly from a listing.

Generate a prep brief for a ranked job:

```bash
uv run job-finger brief in-example --out workspace/briefs/in-example.md
```

The `estimated_fit_probability` value is an explainable fit proxy, not a
statistical hiring probability. It is designed to sort the queue and expose why a
job is worth applying to.

## Frequently Asked Questions

---
**Q: Why is Indeed giving unrelated roles?**  
**A:** Indeed searches the description too.

- use - to remove words
- "" for exact match

Example of a good Indeed query

```py
search_term='"engineering intern" software summer (java OR python OR c++) 2025 -tax -marketing'
```

This searches the description/title and must include software, summer, 2025, one of the languages, engineering intern exactly, no tax, no marketing.

---

**Q: No results when using "google"?**  
**A:** You have to use super specific syntax. Search for google jobs on your browser and then whatever pops up in the google jobs search box after applying some filters is what you need to copy & paste into the google_search_term. 

---

**Q: Received a response code 429?**  
**A:** This indicates that you have been blocked by the job board site for sending too many requests. All of the job board sites are aggressive with blocking. We recommend:

- Wait some time between scrapes (site-dependent).
- Try using the proxies param to change your IP address.

---

### JobPost Schema

```plaintext
JobPost
├── title
├── company
├── company_url
├── job_url
├── location
│   ├── country
│   ├── city
│   ├── state
├── is_remote
├── description
├── job_type: fulltime, parttime, internship, contract
├── job_function
│   ├── interval: yearly, monthly, weekly, daily, hourly
│   ├── min_amount
│   ├── max_amount
│   ├── currency
│   └── salary_source: direct_data, description (parsed from posting)
├── date_posted
└── emails

Linkedin specific
└── job_level

Linkedin & Indeed specific
└── company_industry

Indeed specific
├── company_country
├── company_addresses
├── company_employees_label
├── company_revenue_label
├── company_description
└── company_logo

Naukri specific
├── skills
├── experience_range
├── company_rating
├── company_reviews_count
├── vacancy_count
└── work_from_home_type
```
