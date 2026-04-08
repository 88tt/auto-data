"""
Clean and categorize scraped course data. Reads from the latest raw_data/<CITY>/<YEAR_AND_SEASON>/scrapedYYYYMMDD/ (script 1 output).
Writes dfcrs.csv in that folder for script 3. Uses config.py for CITY, YEAR_AND_SEASON, season (= latest scrape run).

To run:
```
python 2_clean_data.py
```

To run only a specific activity:
```
python 2_clean_data.py ACTIVITIES="Swim,Skate"
```
"""
import os
import re

import numpy as np
import pandas as pd

import config

# ---------------------------------------------------------------------------
# Config: from config.py (same as scripts 1, 3); override with env vars
# ---------------------------------------------------------------------------
CITY = config.CITY
YEAR_AND_SEASON = config.YEAR_AND_SEASON
RAW_DATA_DIR = config.RAW_DATA_DIR
season = config.season

# Which activities to process. Set ACTIVITIES="CampTO,Swim" to run only those; unset = all.
ALL_ACTIVITIES = [
    "Swim", "Skate -", "Ski/Snowboard - ", "Sports -", "Hobbies", "Arts -",
    "Leadership", "CampTO", "Early Years", "FitnessTO", "After School",
    "Adapted Activities",
]
_activities_env = os.environ.get("ACTIVITIES", "").strip()
ACTIVITIES = [a.strip() for a in _activities_env.split(",") if a.strip()] if _activities_env else ALL_ACTIVITIES

# load all course families for each activity to create dfcrs
dfcrs = pd.DataFrame()
dfdesc = pd.DataFrame()
for i, activity in enumerate(ACTIVITIES):
    crs_fams = [f for f in os.listdir(season) if activity in f]
    for j, series in enumerate(crs_fams):
        print('working on ', series)
        # load course info
        activity = activity.replace(' -','')
        try:
            df = pd.read_csv(f'{season}/{series}/courses.csv')
            df['program'] = activity
            df['series'] = series
            dfcrs = pd.concat([dfcrs, df], ignore_index=True)
        except FileNotFoundError:
            print(f'File not found: {season}/{series}/courses.csv')
            continue
        except pd.errors.EmptyDataError:
            print(f'Empty data file: {season}/{series}/courses.csv')
            continue
        # load course description 
        try:
            dfdes = pd.read_csv(f'{season}/{series}/descriptions.csv')
            dfdes['series'] = series
            dfdes['program'] = activity
            dfdesc = pd.concat([dfdesc, dfdes], ignore_index=True)
        except FileNotFoundError:
            print(f'File not found: {season}/{series}/descriptions.csv')
            continue
        except pd.errors.EmptyDataError:
            print(f'Empty data file: {season}/{series}/descriptions.csv')
            continue

if dfcrs.empty:
    raise ValueError(f"No courses loaded from {season}. Check ACTIVITIES and scraped outputs.")

dfcrs = dfcrs.merge(dfdesc, on=['program','series','Name'], how='left')

# initiate categoirzation: defaulting to Name
dfcrs['Name'] = dfcrs['Name'].str.strip().str.lower().str.replace(r'\s+', ' ', regex=True)
dfcrs['crs_name'] = dfcrs['Name']

###################################################
# categorize courses: Swim
###################################################

# for activity=='Swim' and series contains 'Adult/Guardian/Ultra Swim': crs_name should be Name without substring after '-' or substring inside ()
target_series = (dfcrs['program']=='Swim') & (dfcrs['series'].str.contains('Adult Swim|Guardian Swim|Ultra Swim|Preschool',case=False, regex=True))
dfcrs.loc[target_series, 'crs_name'] = dfcrs.loc[target_series, 'Name'].apply(lambda x: x.split('-')[0].strip()).apply(lambda x: x.split('(')[0].strip())

# remove 'Swim -' from series name
dfcrs.loc[dfcrs['program']=='Swim', 'series'] = dfcrs.loc[dfcrs['program']=='Swim', 'series'].str.replace('Swim -', '').str.strip()

# # clean data: inconsisteny naming
# dfcrs.loc[dfcrs['crs_name']== 'swim', 'crs_name']='ultra swim 3'

# small group ultra swim and adult swim should be under their respective series
target_group = (dfcrs['series']=='Small Group-Semi-Private Lessons') & dfcrs['Name'].str.contains('ultra swim', case=False, regex=True)
dfcrs.loc[target_group, 'series'] = 'Ultra Swim 1-9'
dfcrs.loc[target_group, 'crs_name'] = dfcrs.loc[target_group,'Name'].apply(lambda x: x.split('-')[0].strip())

target_group = (dfcrs['series']=='Small Group-Semi-Private Lessons') & (dfcrs['Name'].str.contains('adult swim', case=False, regex=True))
dfcrs.loc[target_group, 'series'] = 'Adult and Older Adult Swim 1-3'
dfcrs.loc[target_group, 'crs_name'] = dfcrs.loc[target_group, 'Name'].apply(lambda x: x.split('-')[0].strip())

# private
dfcrs.loc[dfcrs['series']=='Small Group-Semi-Private Lessons', 'series'] = 'Private Swim Lessons'

# code for checking
'''
dfcrs[dfcrs['program']=='Swim'].groupby(['series','crs_name'])['Course Number'].count().reset_index().sort_values(['series','crs_name'])
dfcrs[dfcrs['program']=='Swim'].groupby(['series','crs_name','Name'])['Course Number'].count().reset_index().sort_values(['series','crs_name'])
'''
                                                                                      
###################################################
# categorize courses: Sports
###################################################
# remove 'Sports -' from series name
dfcrs.loc[dfcrs['program']=='Sports', 'series'] = dfcrs.loc[dfcrs['program']=='Sports', 'series'].str.replace('Sports -', '').str.strip()


'''
dfcrs[dfcrs['program']=='Sports'].groupby(['series','crs_name'])['Course Number'].count().reset_index().sort_values(['series','Course Number'], ascending=False)
dfcrs[dfcrs['program']=='Sports'].groupby(['series','Name','crs_name'])['Course Number'].count().reset_index().sort_values(['series','crs_name'], ascending=True)
dfcrs[dfcrs['program']=='Sports'].groupby(['series'])['Course Number'].count().reset_index().sort_values(['Course Number'], ascending=False)
'''

###################################################
# categorize courses: Arts
###################################################
# for 'Arts - Music' series: recategorize 
def categorize_music(x):
    if 'piano' in x:
        return 'music: piano'
    elif 'keyboard' in x:
        return 'music: keyboard'
    elif 'guitar' in x:
        return 'music: guitar'
    elif 'drum' in x:
        return 'music: drum'
    else:
        return 'music: other'

dfcrs.loc[dfcrs['series'].str.contains('Music'), 'series'] = dfcrs.loc[dfcrs['series'].str.contains('Music'), 'Name'].apply(categorize_music)

def categorize_arts(x):
    if 'painting' in x:
        return 'painting'
    elif 'pottery' in x or 'clay' in x or 'ceramic' in x or 'sculpture' in x:
        return 'pottery/sculpture'
    else:
        return 'visual arts'
    
dfcrs.loc[dfcrs['series'].str.contains('Visual Arts'), 'series'] = dfcrs.loc[dfcrs['series'].str.contains('Visual Arts'), 'Name'].apply(categorize_arts)

# remove 'Arts -'  from series name & make lower case
dfcrs.loc[dfcrs['program']=='Arts', 'series'] = dfcrs.loc[dfcrs['program']=='Arts', 'series'].str.replace('Arts -', '').str.strip().str.lower()

'''
dfcrs[dfcrs['program']=='Arts'].groupby(['program','series'])['Course Number'].count().reset_index().sort_values(['Course Number'], ascending=False)
dfcrs[(dfcrs['program']=='Arts')].groupby(['program','series','crs_name'])['Course Number'].count().reset_index().sort_values(['series','Course Number'], ascending=False)
dfcrs[(dfcrs['program']=='Arts') & (dfcrs['series']=='painting')].groupby(['program','series','crs_name'])['Course Number'].count().reset_index().sort_values(['series','Course Number'], ascending=False)
'''
###################################################
# categorize courses: Hobbies   
###################################################
# remove 'Hobbies and Interests -' from series name
dfcrs.loc[dfcrs['program']=='Hobbies', 'series'] = dfcrs.loc[dfcrs['program']=='Hobbies', 'series'].str.replace('Hobbies and Interests -', '').str.strip()

'''
dfcrs[dfcrs['program']=='Hobbies'].groupby(['series'])['Course Number'].count().reset_index().sort_values(['Course Number'], ascending=False)
dfcrs[dfcrs['program']=='Hobbies'].groupby(['series','crs_name'])['Course Number'].count().reset_index().sort_values(['series','Course Number'], ascending=False)
'''

#################################################################
# categorize courses: Skate/ ski/ snowboard
#################################################################
target_series = (dfcrs['program']=='Skate')|(dfcrs['program'].str.contains('Ski'))
# exclude () & substring inside  
dfcrs.loc[target_series, 'crs_name'] = dfcrs.loc[target_series, 'Name'].apply(lambda x: re.sub(r'\(.*?\)', '', x).strip())

# for ski/snowboard: new series names: adult ski series, adult snowboard series, ski series, snowboard series 
def categorize_ski_snowboard(x):
    # return 'adult ski series' if x has this pattern: adult ski {any number}
    if re.search(r'adult ski \d+', x, re.IGNORECASE):
        return 'adult ski'  
    # return 'adult snowboard series' if x has this pattern: adult snowboard {any number}
    elif re.search(r'adult snowboard \d+', x, re.IGNORECASE):
        return 'adult snowboard'
    # return 'ski series' if x has this pattern: ski {any number}
    elif re.search(r'ski \d+', x, re.IGNORECASE):
        return 'child ski'
    # return 'snowboard series' if x has this pattern: snowboard {any number}
    elif re.search(r'snowboard \d+', x, re.IGNORECASE):
        return 'child snowboard'
    # if 'caregiver' in x, return 'ski with caregiver'
    elif 'ski race' in x:
        return 'ski race'
    elif 'caregiver' in x and ('ski' in x or 'snowboard' in x):
        return 'ski with caregiver'
    elif re.search(r'learn to skate', x, re.IGNORECASE) or re.search(r'skating', x, re.IGNORECASE):
        return 'skate'
    elif re.search(r'goalie', x, re.IGNORECASE) or re.search(r'shinny', x, re.IGNORECASE) or re.search(r'hockey', x, re.IGNORECASE):
        return 'hockey'
    else:
        return x
    
dfcrs.loc[target_series, 'series'] = dfcrs.loc[target_series, 'Name'].apply(categorize_ski_snowboard)
dfcrs.loc[target_series, 'series'] = dfcrs.loc[target_series, 'series'].apply(lambda x: re.sub(r'\(.*?\)', '', x).strip())

dfcrs.loc[target_series,'program'] = 'Skate & Ski'  

# code for checking
'''
target_series = (dfcrs['program']=='Skate')|(dfcrs['program'].str.contains('Ski'))
dfcrs[target_series].groupby(['series','crs_name'])['Course Number'].count().reset_index().sort_values(['series','crs_name'])
dfcrs[target_series].groupby(['series','Name','crs_name'])['Course Number'].count().reset_index().sort_values(['series','crs_name'])
dfcrs[target_series].groupby(['series'])['Course Number'].count().reset_index().sort_values(['Course Number'], ascending=False)
'''

###################################################
# categorize courses: Leaderships   
###################################################
target_series = dfcrs['program'].str.contains('Leadership', case=False)
dfcrs.loc[target_series, 'program'] = 'Leadership'

'''
dfcrs[target_series].groupby(['series','crs_name'])['Course Number'].count().reset_index().sort_values(['series','crs_name'])
'''

###################################################
# categorize courses: CampTO   
###################################################
target_series = dfcrs['program'].str.contains('CampTO', case=False)

'''
target_series = dfcrs['program'].str.contains('CampTO', case=False)
dfcrs[target_series].groupby(['series','crs_name'])['Course Number'].count().reset_index().sort_values(['series','crs_name'])
'''

###################################################
# categorize courses: FitnessTO   
###################################################
target_series = dfcrs['program'].str.contains('FitnessTO', case=False)

'''
target_series = dfcrs['program'].str.contains('FitnessTO', case=False)
dfcrs[target_series].groupby(['series','crs_name'])['Course Number'].count().reset_index().sort_values(['series','crs_name'])
'''

###################################################
# categorize courses: Early Years   
###################################################
target_series = dfcrs['program'].str.contains('Early Years', case=False)
dfcrs.loc[target_series, 'program'] = 'Early Years & After School'


'''
dfcrs.loc[target_series, 'program'] = 'Early Years & After School'
dfcrs[target_series].groupby(['series','crs_name'])['Course Number'].count().reset_index().sort_values(['series','crs_name'])
'''

###################################################
# categorize courses: Early Years   
###################################################
target_series = dfcrs['program'].str.contains('After School', case=False)
dfcrs.loc[target_series, 'program'] = 'Early Years & After School'

'''
dfcrs[target_series].groupby(['series','crs_name'])['Course Number'].count().reset_index().sort_values(['series','crs_name'])
'''

###################################################
# extract start/ end dates and times
###################################################
# split 'Time' column into 'day_of_week', 'start_time', 'end_time'
dfcrs[['day_of_week', 'start_time', 'end_time']] = dfcrs['Time'].str.extract(r'([A-Za-z,]+)\s*(\d{1,2}\s*:\s*\d{2}\s*[AP]M|Noon)\s*-\s*(\d{1,2}\s*:\s*\d{2}\s*[AP]M|Noon)')
dfcrs['start_time'] = pd.to_datetime(dfcrs['start_time'].str.lower().str.replace('noon','12:00 PM')).dt.time
dfcrs['end_time'] = pd.to_datetime(dfcrs['end_time'].str.lower().str.replace('noon','12:00 PM')).dt.time

# extract start_date and end_date from 'Date' column (e.g. 'January 8, 2025 to March 5, 2025', 'February 7, 2025'). Note that sometimes there is only one date
dfcrs['start_date'] = pd.to_datetime(dfcrs['Date'].str.extract(r'(\w+ \d{1,2}, \d{4})', expand=False))
dfcrs['end_date'] = pd.to_datetime(dfcrs['Date'].str.extract(r'to (\w+ \d{1,2}, \d{4})', expand=False))

# extract min & max age
def _norm_age_text(value):
    if pd.isna(value):
        return ""
    txt = str(value).strip().lower()
    txt = re.sub(r"\s+", " ", txt)
    # Normalize common typos/variants seen in source data.
    txt = txt.replace("leasat", "least")
    txt = re.sub(r"years?\b|yrs?\b", "y", txt)
    txt = re.sub(r"months?\b", "m", txt)
    txt = re.sub(r"(\d{1,3})\s*y\s*(\d{1,3})\s*m", r"\1y \2m", txt)
    return txt


def _parse_ym(fragment):
    if not fragment:
        return np.nan
    # Allow up to 3 digits to support values like 120 years.
    y_match = re.search(r"(\d{1,3})\s*y\b", fragment)
    m_match = re.search(r"(\d{1,3})\s*m\b", fragment)
    if not y_match and not m_match:
        return np.nan
    years = int(y_match.group(1)) if y_match else 0
    months = int(m_match.group(1)) if m_match else 0
    return years + months / 12.0


def _extract_age_bounds(value):
    txt = _norm_age_text(value)
    if not txt:
        return pd.Series({"min_age": 0.0, "max_age": 999.0, "age_parse_issue": False})

    if "all ages" in txt:
        return pd.Series({"min_age": 0.0, "max_age": 999.0, "age_parse_issue": False})

    min_age = np.nan
    max_age = np.nan

    # Parse lower/upper bound from separate fragments to avoid cross-matching.
    if "at least" in txt and "less than" in txt:
        left, right = txt.split("less than", 1)
        min_age = _parse_ym(left)
        max_age = _parse_ym(right)

    if np.isnan(min_age) and "+" in txt:
        min_age = _parse_ym(txt)
        max_age = 999.0

    if np.isnan(min_age) and "at least" in txt:
        min_age = _parse_ym(txt)

    if np.isnan(max_age) and "less than" in txt:
        max_age = _parse_ym(txt.split("less than", 1)[1])

    if np.isnan(min_age):
        min_age = 0.0
    if np.isnan(max_age):
        max_age = 999.0

    issue = bool(max_age != 999.0 and min_age > max_age)
    if issue:
        # Prefer open upper bound over impossible ranges that would hide eligible users.
        max_age = 999.0

    return pd.Series({"min_age": float(min_age), "max_age": float(max_age), "age_parse_issue": issue})


dfcrs["Age"] = dfcrs["Age"].str.replace(r"\s+", " ", regex=True)
_age_bounds = dfcrs["Age"].apply(_extract_age_bounds)
dfcrs["min_age"] = _age_bounds["min_age"]
dfcrs["max_age"] = _age_bounds["max_age"]
_age_issue_count = int(_age_bounds["age_parse_issue"].sum())
if _age_issue_count > 0:
    print(f"WARNING: {_age_issue_count} rows had inconsistent age bounds (min_age > max_age). max_age set to 999 for these rows.")

# Listing availability from scraper (courses.csv); legacy CSVs may omit column
if "availability" not in dfcrs.columns:
    dfcrs["availability"] = "open"
else:
    dfcrs["availability"] = (
        dfcrs["availability"].fillna("open").astype(str).str.strip().str.lower()
    )
    _av_ok = {"full", "open", "unknown"}
    dfcrs.loc[~dfcrs["availability"].isin(_av_ok), "availability"] = "open"

# Whether card had an actionable 'Enroll Now' button; default False for legacy files.
if "has_enroll_now" not in dfcrs.columns:
    dfcrs["has_enroll_now"] = False
else:
    _truthy = {"1", "true", "t", "yes", "y"}
    dfcrs["has_enroll_now"] = (
        dfcrs["has_enroll_now"]
        .fillna(False)
        .apply(lambda v: str(v).strip().lower() in _truthy if not isinstance(v, bool) else v)
    )

# save dfcrs to csv
dfcrs.to_csv(f'{season}/dfcrs.csv', index=False)