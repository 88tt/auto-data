from selenium import webdriver
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import NoSuchElementException, ElementClickInterceptedException, TimeoutException
from bs4 import BeautifulSoup
import json
import re
import time
import pandas as pd
import os
from dotenv import load_dotenv
import requests
from time import sleep
import os
import openai
from openai import OpenAI
from ast import literal_eval

sleep_time = 2
WAIT_TIMEOUT = 30  # seconds for explicit waits (30 for CI, 15 was too short)

# Listing pagination (Toronto Active Communities): "View more" is inside <button><span>
# with newlines/whitespace. XPath `span[text()='View more']` does not match.
VIEW_MORE_BUTTON_XPATH = (
    "//div[contains(@class,'load-more')]//button[.//span[normalize-space()='View more']]"
    " | //span[normalize-space()='View more']/ancestor::button[1]"
)
# Large programs (1000+ rows) need many clicks; each batch can be slow to paint.
MAX_VIEW_MORE_CLICKS = 80
VIEW_MORE_BUTTON_WAIT = 15
# After each click: wait up to this long for new rows; poll frequently so fast batches finish quickly.
POST_CLICK_MAX_WAIT = 20
POST_CLICK_POLL_FREQUENCY = 0.4
STALL_LIMIT = 3
# Longer tail settle only when the page reports a large total (many async card inserts).
SETTLE_LARGE_LIST_THRESHOLD = 250
SETTLE_AFTER_PAGINATION_LARGE_SEC = 2.5
SETTLE_AFTER_PAGINATION_SMALL_SEC = 1.0


def _parse_load_more_progress(page_source: str) -> tuple[int | None, int | None]:
    """e.g. 'You have viewed 1060 out of 1312 results.' -> (1060, 1312)."""
    m = re.search(
        r"viewed\s+(\d+)\s+out\s+of\s+(\d+)\s+results",
        page_source,
        re.IGNORECASE,
    )
    if not m:
        return None, None
    return int(m.group(1)), int(m.group(2))


def _load_more_growth_seen(
    driver,
    before_cards: int,
    before_viewed: int | None,
) -> bool:
    """
    True when the listing has progressed after a 'View more' click.

    Prefer cheap regex on page_source ('viewed X out of Y') before counting DOM nodes.
    """
    src = driver.page_source
    v2, t2 = _parse_load_more_progress(src)
    if v2 is not None and t2 is not None and v2 >= t2:
        return True
    if before_viewed is not None and v2 is not None and v2 > before_viewed:
        return True
    return (
        len(driver.find_elements(By.CSS_SELECTOR, "div.activity-container")) > before_cards
    )


def click_view_more_until_exhausted(driver) -> None:
    """
    Scroll and click 'View more' until the listing is complete or we give up.

    Uses the page's 'viewed X out of Y results' text when present so we do not
    stop early on slow batches (large lists used to quit after ~2.5s with no growth).
    """
    stalls = 0
    for _ in range(MAX_VIEW_MORE_CLICKS):
        html = driver.page_source
        viewed, total_results = _parse_load_more_progress(html)
        if (
            viewed is not None
            and total_results is not None
            and viewed >= total_results
        ):
            break

        try:
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(0.4)
            view_more_button = WebDriverWait(driver, VIEW_MORE_BUTTON_WAIT).until(
                EC.element_to_be_clickable((By.XPATH, VIEW_MORE_BUTTON_XPATH))
            )
        except (TimeoutException, NoSuchElementException):
            break

        before_cards = len(driver.find_elements(By.CSS_SELECTOR, "div.activity-container"))
        before_viewed = viewed

        driver.execute_script(
            "arguments[0].scrollIntoView({block: 'center'});", view_more_button
        )
        time.sleep(0.25)

        try:
            view_more_button.click()
        except ElementClickInterceptedException:
            stalls += 1
            if stalls >= STALL_LIMIT:
                break
            time.sleep(2.5)
            continue

        try:
            WebDriverWait(
                driver,
                POST_CLICK_MAX_WAIT,
                poll_frequency=POST_CLICK_POLL_FREQUENCY,
            ).until(
                lambda d: _load_more_growth_seen(d, before_cards, before_viewed)
            )
            grew = True
        except TimeoutException:
            grew = _load_more_growth_seen(driver, before_cards, before_viewed)

        if grew:
            stalls = 0
        else:
            stalls += 1
            if stalls >= STALL_LIMIT:
                break

    _, total_hint = _parse_load_more_progress(driver.page_source)
    n_cards = len(driver.find_elements(By.CSS_SELECTOR, "div.activity-container"))
    if (
        total_hint is not None
        and total_hint >= SETTLE_LARGE_LIST_THRESHOLD
    ) or n_cards >= SETTLE_LARGE_LIST_THRESHOLD:
        time.sleep(SETTLE_AFTER_PAGINATION_LARGE_SEC)
    else:
        time.sleep(SETTLE_AFTER_PAGINATION_SMALL_SEC)


def _parse_activity_categories_from_html(html):
    """Extract activity_categories array from embedded JSON in page source. Returns list of dicts with 'title' and 'url'."""
    m = re.search(r'"activity_categories":\s*(\[)', html)
    if not m:
        return []
    start = m.end(1) - 1
    depth = 0
    for i in range(start, len(html)):
        if html[i] == "[":
            depth += 1
        elif html[i] == "]":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[start : i + 1])
                except json.JSONDecodeError:
                    return []
    return []


def initiate_and_get_all_activities(site_slug="toronto"):
    """Open browser and load activity search page; activity list comes from embedded JSON (new site: no hover menu)."""
    options = webdriver.ChromeOptions()
    if os.environ.get("CHROME_HEADLESS", "").strip().lower() in {"1", "true", "yes"}:
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")
        options.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)
    driver = webdriver.Chrome(options=options)
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    })
    base = "https://anc.ca.apm.activecommunities.com"
    # New site: use activity search page; activity list is in embedded state, not hover dropdown
    url = f"{base}/{site_slug}/activity/search?onlineSiteId=0"
    for attempt in range(3):
        try:
            driver.get(url)
            break
        except Exception as e:
            if attempt == 2:
                raise
            time.sleep(2)

    # Wait for filter/search UI to be present (new page structure)
    WebDriverWait(driver, WAIT_TIMEOUT).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, ".search-group__when, .activity-results-header__total, [class*='search-group']"))
    )
    time.sleep(1)  # allow embedded state to render

    # Get activity names from embedded JSON (activity_categories)
    categories = _parse_activity_categories_from_html(driver.page_source)
    programs = [c["title"] for c in categories if c.get("title")]
    if not programs:
        # Fallback: try old home-page structure (hover menu) if new page has no categories
        driver.get(f"{base}/{site_slug}/home?onlineSiteId=0&from_original_cui=true")
        WebDriverWait(driver, WAIT_TIMEOUT).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "section[aria-label='ACTIVITY CATEGORIES']"))
        )
        soup = BeautifulSoup(driver.page_source, "html.parser")
        sections = soup.find_all("section", attrs={"aria-label": "ACTIVITY CATEGORIES"})
        programs = []
        for i in sections:
            nav = i.find_all("div", class_="nav-secondary-menu-column")
            for j in nav:
                ul = j.find("ul")
                if ul:
                    for k in ul.find_all("li"):
                        a = k.find("a")
                        if a and a.find("span"):
                            programs.append(a.find("span").text)

    return programs, driver


def choose_activity(driver, activity_name):
    """Select activity: new site uses Activity filter + 'more' or direct URL from embedded JSON. Prefer URL navigation."""
    html = driver.page_source
    categories = _parse_activity_categories_from_html(html)
    url_for_activity = None
    for c in categories:
        if c.get("title") == activity_name and c.get("url"):
            url_for_activity = c["url"]
            break

    if url_for_activity:
        driver.get(url_for_activity)
        WebDriverWait(driver, WAIT_TIMEOUT).until(
            EC.presence_of_element_located((By.CLASS_NAME, "activity-results-header__total"))
        )
    else:
        # Fallback for menu-driven UI variants:
        # 1) Try hover/click on top-nav "Activities" and click submenu item by normalized text.
        # 2) If not found, fall back to search-page filter flow and resilient link text matching.
        safe_name = _xpath_escape(activity_name.strip())
        try:
            activities_nav = WebDriverWait(driver, 8).until(
                EC.presence_of_element_located(
                    (
                        By.XPATH,
                        "//a[normalize-space()='Activities' or normalize-space(.)='Activities']",
                    )
                )
            )
            ActionChains(driver).move_to_element(activities_nav).pause(0.3).perform()
            try:
                activities_nav.click()
            except Exception:
                pass

            submenu_item = WebDriverWait(driver, 6).until(
                EC.element_to_be_clickable(
                    (
                        By.XPATH,
                        f"//a[normalize-space()='{safe_name}' or .//span[normalize-space()='{safe_name}']]",
                    )
                )
            )
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", submenu_item)
            driver.execute_script("arguments[0].click();", submenu_item)
        except (NoSuchElementException, TimeoutException):
            activities_link = WebDriverWait(driver, WAIT_TIMEOUT).until(
                EC.element_to_be_clickable((By.LINK_TEXT, "Activities"))
            )
            activities_link.click()
            time.sleep(0.5)
            try:
                more_btn = WebDriverWait(driver, 5).until(
                    EC.element_to_be_clickable((By.XPATH, "//button[contains(.,'more')] | //a[contains(@aria-label,'more')] | //*[contains(text(),'View more') or contains(text(),'More')]"))
                )
                more_btn.click()
                time.sleep(0.5)
            except (NoSuchElementException, TimeoutException):
                pass
            try:
                activity_link = driver.find_element(By.LINK_TEXT, activity_name)
            except NoSuchElementException:
                activity_link = driver.find_element(
                    By.XPATH,
                    f"//a[normalize-space()='{safe_name}' or .//span[normalize-space()='{safe_name}']]",
                )
            driver.execute_script("arguments[0].click();", activity_link)

        WebDriverWait(driver, WAIT_TIMEOUT).until(
            EC.presence_of_element_located((By.CLASS_NAME, "activity-results-header__total"))
        )

    try:
        total_el = driver.find_element(By.CLASS_NAME, "activity-results-header__total")
        total_courses = total_el.find_element(By.TAG_NAME, "b").text
    except NoSuchElementException:
        total_courses = "0"
    return total_courses

def get_activity_location(driver, activity_name):
    # click on the desired activity
    total_courses = choose_activity(driver, activity_name)

    # click on the button that contains text 'Where' to expose locations for that activity
    driver.find_element(By.XPATH, "//button[span[span[text()='Where ']]]").click()

    # click <a> with aria label 'View more' to show all locations
    try:
        driver.find_element(By.XPATH, "//a[@aria-label='View more']").click()
        time.sleep(sleep_time)  # Wait for the content to load
    except NoSuchElementException:
        pass

    # get all span with "checkbox__text"
    locations = driver.find_elements(By.CLASS_NAME, 'checkbox__text')

    # store the text of each span in a list
    locations_text = [location.text for location in locations]
    
    return locations_text, total_courses

def choose_activity_and_location(driver, activity_name, location=None):
    if location:
        locations_text, total_courses = get_activity_location(driver, activity_name)
        try:
            # click 'Reset' to clear previous selection
            driver.find_element(By.XPATH, "//a[span[text()='Reset']]").click()
        except NoSuchElementException:
            pass

        # click check box with text desired location with class 'checkbox__text'
        driver.find_element(By.XPATH, f"//span[text()='{location}']")
        parent_span = driver.find_element(By.XPATH, f"//span[text()='{location}']/..")
        # find the sibling input element and click it
        sibling_input = parent_span.find_element(By.XPATH, "preceding-sibling::input")
        sibling_input.click()

        # click button with 'Apply' text
        driver.find_element(By.XPATH, "//button[span[span[text()='Apply']]]").click()
        time.sleep(sleep_time)  # Wait for the content to load
    else:
        total_courses = choose_activity(driver, activity_name)

    click_view_more_until_exhausted(driver)

    return total_courses


def _availability_from_activity_container(activity):
    """
    Listing enrollment ribbon on each card (Toronto Active Communities HTML).
    Verified in saved page_source.txt: activity-card__cornerMark--Full, --unknown.
    Returns: 'full' | 'open' | 'unknown'
    """
    for div in activity.find_all("div", class_=True):
        classes = div.get("class") or []
        if "activity-card__cornerMark" not in classes:
            continue
        joined = " ".join(classes)
        if "activity-card__cornerMark--Full" in joined:
            return "full"
        if "activity-card__cornerMark--unknown" in joined:
            return "unknown"
        # Other modifiers (e.g. future waitlist): treat as unknown until explicitly mapped
        return "unknown"
    return "open"


def _has_enroll_now_button(activity):
    """
    Detect whether a listing card shows an actionable 'Enroll Now' button/link.
    Returns bool.
    """
    for tag in activity.find_all(["a", "button"]):
        text = tag.get_text(" ", strip=True)
        if text and re.search(r"\benroll\s+now\b", text, re.IGNORECASE):
            return True
    return False


def get_course_info(driver):
    # get page source; identify unique courses; get course info
    soup = BeautifulSoup(driver.page_source, 'html.parser')

    # get all <dvi> with class="activity-container"
    activities = soup.find_all('div', class_='activity-container')

    # for each activity, get the name, props, location, datetime. Below are the classes to look for. store in a dataframe
    # initialize an empty list to store all activities
    crs_info = []

    for activity in activities:
        name        = activity.find('div', class_='activity-card-info__name').find('a').find('span').text
        url         = activity.find('div', class_='activity-card-info__name').find('a').get('href')
        crs_num     = activity.find('div', class_='activity-card-info__props').find('span', class_='activity-card-info__number').find('span').text
        age         = activity.find('div', class_='activity-card-info__props').find('span', class_='activity-card-info__ages').text
        location    = activity.find('div', class_='activity-card-info__location').find('span').text
        date        = activity.find('div', class_='activity-card-info__datetime').find('span', class_='activity-card-info__dateRange').text
        time_range  = activity.find('div', class_='activity-card-info__datetime').find('span', class_='activity-card-info__timeRange').text
        availability = _availability_from_activity_container(activity)
        has_enroll_now = _has_enroll_now_button(activity)

        data = {
            'Name': name,
            'URL': url,
            'Course Number': crs_num,
            'Age': age,
            'Location': location,
            'Date': date,
            'Time': time_range,
            'availability': availability,
            'has_enroll_now': has_enroll_now,
        }

        crs_info.append(data)

    return pd.DataFrame(crs_info)

def _xpath_escape(text):
    """Escape double quotes for use inside XPath string literal (double them)."""
    return (text or "").replace('"', '""')


def get_course_description(driver, df):
    try:
        unique_crs = df['Name'].unique()
    except Exception as e:
        print(f'No unique courses found in the dataframe: {e}. Returning empty DataFrame.')
        return pd.DataFrame(columns=['Name', 'Description'])

    tooltip_id = 'activity-card-info__tooltip-msg'
    course_desc = []
    for course_name in unique_crs:
        safe_name = _xpath_escape(course_name)
        try:
            crs_link = driver.find_element(
                By.XPATH,
                f'//div[@class="activity-card-info__name-link"]//a[span[text()="{safe_name}"]]'
            )
        except NoSuchElementException:
            safe_stripped = _xpath_escape(course_name.strip())
            crs_link = driver.find_element(
                By.XPATH,
                f'//div[@class="activity-card-info__name-link"]//a[span[normalize-space(text())="{safe_stripped}"]]'
            )
        parent = crs_link.find_element(By.XPATH, "./..")
        info_button = parent.find_element(By.XPATH, "./following-sibling::span")
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", info_button)
        info_button.click()

        WebDriverWait(driver, WAIT_TIMEOUT).until(
            EC.visibility_of_element_located((By.ID, tooltip_id))
        )
        soup = BeautifulSoup(driver.page_source, 'html.parser')
        el = soup.find('span', id=tooltip_id)
        description = el.text if el else ""

        close_btn = WebDriverWait(driver, WAIT_TIMEOUT).until(
            EC.element_to_be_clickable((By.XPATH, "//button[span[text()='Close']]"))
        )
        close_btn.click()
        WebDriverWait(driver, 3).until(
            EC.invisibility_of_element_located((By.ID, tooltip_id))
        )

        course_desc.append({'Name': course_name, 'Description': description})

    return pd.DataFrame(course_desc)

# API call to OpenAI to get course description
def get_crs_name_desc(prompt):
    api_key = os.getenv('OPENAI_API_KEY')
    if not api_key:
        raise ValueError("No API key found. Please set the MY_API_KEY environment variable.")    
    
    client = OpenAI()
    completion = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "You are a helpful assistant for summarizing course descriptions. You will be given a series of related courses that you will summarize in one concise paragraph to create a series level summary"},
            {
                "role": "user",
                "content": prompt
            }
        ]
    )
    print('done')
    return completion.choices[0].message

# API call to Google Places to get place ID
def get_place_id(place_name):
    google_api_key = os.getenv('GOOGLE_MAP_API_KEY')
    if not google_api_key:
        raise ValueError("GOOGLE_MAP_API_KEY is not set. Set it in the environment or .env.")

    if not place_name or not str(place_name).strip():
        return None

    url = 'https://maps.googleapis.com/maps/api/place/findplacefromtext/json'
    params = {
        'input': place_name,
        'inputtype': 'textquery',
        'fields': 'place_id',
        'key': google_api_key
    }
    print(f'Finding place ID for {place_name}..')
    response = requests.get(url, params=params)
    sleep(2)
    if response.status_code != 200:
        print(f'Place ID request failed: status {response.status_code}')
        return None
    data = response.json()
    if data.get('error'):
        print(f'Place ID API error: {data.get("error")}')
        return None
    if data.get('candidates'):
        place_id = data['candidates'][0]['place_id']
        print(f'Found place ID: {place_id}')
        return place_id
    return None

# API call to Google Places to get place details
def get_place_details(place_id):
    if place_id is None or not str(place_id).strip():
        return None

    google_api_key = os.getenv('GOOGLE_MAP_API_KEY')
    if not google_api_key:
        raise ValueError("GOOGLE_MAP_API_KEY is not set. Set it in the environment or .env.")

    url = 'https://maps.googleapis.com/maps/api/place/details/json'
    params = {
        'place_id': place_id,
        'fields': 'name,formatted_address,geometry,types,website,opening_hours,rating,review,formatted_phone_number,international_phone_number,photos,price_level,user_ratings_total',
        'key': google_api_key
    }
    print(f'Getting details for place ID {place_id}..')
    response = requests.get(url, params=params)
    sleep(2)
    if response.status_code != 200:
        print(f'Place details request failed: status {response.status_code}')
        return None
    data = response.json()
    if data.get('error'):
        print(f'Place details API error: {data.get("error")}')
        return None
    if data.get('result'):
        return data['result']
    print('No result found for place_id')
    return None

def parse_place_details(details):
    """Return (name, address, latitude, longitude, url, type). Returns 6-tuple of Nones if details is missing or malformed."""
    _none = (None, None, None, None, None, None)
    if not details:
        return _none
    try:
        name = details.get('name')
        address = details.get('formatted_address')
        geo = details.get('geometry') or {}
        loc = geo.get('location') or {}
        latitude = loc.get('lat')
        longitude = loc.get('lng')
        type_ = details.get('types')
        url = details.get('website')
        if name is None and address is None and latitude is None and longitude is None:
            return _none
        return (name, address, latitude, longitude, url, type_)
    except (TypeError, AttributeError, KeyError) as e:
        print(f'parse_place_details failed: {e}')
        return _none

def get_coordinates(address):
    url = 'https://maps.googleapis.com/maps/api/geocode/json'
    params = {'address': address, 'key': os.getenv('GOOGLE_MAP_API_KEY') }
    print(f'working on {address}..')
    response = requests.get(url, params=params)
    # print('got', response.json()['results'][0]['formatted_address'])
    sleep(2)
    if response.status_code == 200:
        data = response.json()
        return data
    return None

def parse_geo_google_details(details):
    try:
        latitude = details['results'][0]['geometry']['location']['lat']
        longitude = details['results'][0]['geometry']['location']['lng']
        address = details['results'][0]['formatted_address']
        type = details['results'][0]['types']
        return address, latitude, longitude, type
    except KeyError:
        return None, None, None ,None
    
def parse_business_details(details):
    if details:
        address = details['formatted_address']
        latitude = details['geometry']['location']['lat']
        longitude = details['geometry']['location']['lng']
        return address, latitude, longitude
    return None