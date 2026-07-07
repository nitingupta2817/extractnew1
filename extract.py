import streamlit as st
import requests
from bs4 import BeautifulSoup
import re
import json
import time
import pandas as pd
from io import BytesIO
from urllib.parse import urljoin, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

# ==================================================================
# CONFIG
# ==================================================================
EMAIL_PATTERN = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
PHONE_PATTERN = r'(?:(?:\+?\d{1,3}[\s.-]?)?(?:\(?\d{2,4}\)?[\s.-]?)?\d{3,4}[\s.-]?\d{3,4}(?:[\s.-]?\d{2,4})?)'
COMMON_CONTACT_PATHS = ["", "/contact", "/contact-us", "/about", "/about-us", "/support", "/team"]
REQUEST_TIMEOUT = 10
DEFAULT_MAX_WORKERS = 8
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; LeadFinderBot/1.0; +https://example.com/bot)"
}
JUNK_EXTENSIONS = (
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp",
    ".css", ".js", ".woff", ".woff2", ".ttf", ".eot"
)
CONTACT_TITLE_HINTS = (
    "director", "manager", "ceo", "founder", "owner", "proprietor", "head",
    "coordinator", "contact person", "sales", "marketing", "hr", "president",
    "managing director", "md", "cfo", "coo", "representative"
)
SERPAPI_ENDPOINT = "https://serpapi.com/search"


# ==================================================================
# VALIDATORS / HELPERS
# ==================================================================
def is_valid_email(email: str) -> bool:
    email = email.lower().strip()
    if any(email.endswith(ext) for ext in JUNK_EXTENSIONS):
        return False
    if re.search(r'\.(png|jpg|jpeg|gif|svg|webp)$', email):
        return False
    if email.count("@") != 1:
        return False
    return True


def is_valid_phone(phone: str) -> bool:
    digits = re.sub(r'\D', '', phone)
    return 7 <= len(digits) <= 14


def get_domain(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def normalize_url(url: str) -> str:
    url = url.strip()
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def fetch_page(url: str, retries: int = 2):
    last_exc = None
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            return resp
        except Exception as e:
            last_exc = e
            time.sleep(0.5 * (attempt + 1))
    raise last_exc


def discover_candidate_pages(base_url: str):
    domain = get_domain(base_url)
    candidates = [urljoin(domain, path) for path in COMMON_CONTACT_PATHS]
    seen, ordered = set(), []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            ordered.append(c)
    return ordered


# ==================================================================
# FIELD EXTRACTORS
# ==================================================================
def extract_json_ld_blocks(soup: BeautifulSoup):
    blocks = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "{}")
        except Exception:
            continue
        blocks.extend(data if isinstance(data, list) else [data])
    return [b for b in blocks if isinstance(b, dict)]


def extract_address_from_soup(soup: BeautifulSoup, json_ld_blocks=None):
    json_ld_blocks = json_ld_blocks if json_ld_blocks is not None else extract_json_ld_blocks(soup)

    for entry in json_ld_blocks:
        addr = entry.get("address")
        if isinstance(addr, dict):
            parts = [
                addr.get("streetAddress", ""), addr.get("addressLocality", ""),
                addr.get("addressRegion", ""), addr.get("postalCode", ""),
                addr.get("addressCountry", ""),
            ]
            joined = ", ".join(p for p in parts if p)
            if joined:
                return joined
        elif isinstance(addr, str) and addr.strip():
            return addr.strip()

    addr_tag = soup.find("address")
    if addr_tag and addr_tag.get_text(strip=True):
        return addr_tag.get_text(separator=", ", strip=True)[:300]

    for el in soup.find_all(attrs={"class": re.compile(r'(address|location)', re.I)}):
        text = el.get_text(separator=", ", strip=True)
        if text and 10 < len(text) < 300:
            return text

    return ""


def extract_company_name_from_soup(soup: BeautifulSoup, json_ld_blocks=None, fallback_url: str = ""):
    json_ld_blocks = json_ld_blocks if json_ld_blocks is not None else extract_json_ld_blocks(soup)

    for entry in json_ld_blocks:
        entry_type = entry.get("@type", "")
        types = entry_type if isinstance(entry_type, list) else [entry_type]
        if any(t in ("Organization", "Corporation", "LocalBusiness") for t in types):
            name = entry.get("name")
            if name:
                return name.strip()

    og_site = soup.find("meta", property="og:site_name")
    if og_site and og_site.get("content"):
        return og_site["content"].strip()

    if soup.title and soup.title.get_text(strip=True):
        title = soup.title.get_text(strip=True)
        title = re.split(r'[|\-–—•]', title)[0].strip()
        if title:
            return title

    if fallback_url:
        return urlparse(fallback_url).netloc.replace("www.", "")

    return ""


def extract_contact_person_from_soup(soup: BeautifulSoup, json_ld_blocks=None):
    json_ld_blocks = json_ld_blocks if json_ld_blocks is not None else extract_json_ld_blocks(soup)

    for entry in json_ld_blocks:
        entry_type = entry.get("@type", "")
        types = entry_type if isinstance(entry_type, list) else [entry_type]
        if "Person" in types:
            name = entry.get("name")
            title = entry.get("jobTitle", "")
            if name:
                return f"{name} ({title})" if title else name

        for key in ("founder", "employee"):
            person = entry.get(key)
            if isinstance(person, dict) and person.get("name"):
                return person["name"]
            if isinstance(person, list):
                for p in person:
                    if isinstance(p, dict) and p.get("name"):
                        return p["name"]

    text = soup.get_text(separator="\n")
    for line in text.splitlines():
        line = line.strip()
        if not line or len(line) > 100:
            continue
        lower = line.lower()
        if any(hint in lower for hint in CONTACT_TITLE_HINTS):
            match = re.search(r'[:\-–]\s*([A-Z][a-zA-Z.\'\-]+(?:\s+[A-Z][a-zA-Z.\'\-]+){0,2})\s*$', line)
            if match:
                candidate = match.group(1).strip()
                if 4 <= len(candidate) <= 40:
                    return candidate

    return ""


def extract_contact_info_from_html(html: str, url: str = ""):
    soup = BeautifulSoup(html, "html.parser")
    json_ld_blocks = extract_json_ld_blocks(soup)

    mailtos = [
        a["href"][7:].split("?")[0]
        for a in soup.find_all("a", href=True)
        if a["href"].lower().startswith("mailto:")
    ]
    text = soup.get_text(separator=" ")
    emails_in_text = re.findall(EMAIL_PATTERN, text)
    emails = {e for e in set(mailtos + emails_in_text) if is_valid_email(e)}

    tel_links = [
        a["href"][4:].strip()
        for a in soup.find_all("a", href=True)
        if a["href"].lower().startswith("tel:")
    ]
    phones_in_text = re.findall(PHONE_PATTERN, text)
    phones = {p.strip() for p in (tel_links + phones_in_text) if is_valid_phone(p)}

    return {
        "emails": emails,
        "phones": phones,
        "address": extract_address_from_soup(soup, json_ld_blocks),
        "company_name": extract_company_name_from_soup(soup, json_ld_blocks, fallback_url=url),
        "contact_person": extract_contact_person_from_soup(soup, json_ld_blocks),
    }


def extract_contact_info_from_website(url: str, crawl_extra_pages: bool = True):
    url = normalize_url(url)
    pages_to_check = discover_candidate_pages(url) if crawl_extra_pages else [url]

    all_emails, all_phones = set(), set()
    address, company_name, contact_person = "", "", ""
    pages_checked, errors = [], []

    for page_url in pages_to_check:
        try:
            resp = fetch_page(page_url)
            info = extract_contact_info_from_html(resp.text, url=url)
            all_emails.update(info["emails"])
            all_phones.update(info["phones"])
            if info["address"] and not address:
                address = info["address"]
            if info["company_name"] and not company_name:
                company_name = info["company_name"]
            if info["contact_person"] and not contact_person:
                contact_person = info["contact_person"]
            pages_checked.append(page_url)
        except Exception as e:
            errors.append((page_url, str(e)))
            if page_url == url:
                break

        if all_emails and all_phones and address and contact_person:
            break

    return {
        "emails": all_emails, "phones": all_phones, "address": address,
        "company_name": company_name, "contact_person": contact_person,
        "pages_checked": pages_checked, "errors": errors,
    }


# ==================================================================
# SERPAPI SEARCH
# ==================================================================
def search_businesses(api_key: str, query: str, num_results: int):
    results = []
    per_page = 10
    pages_needed = max(1, -(-num_results // per_page))
    total_results_estimate = None

    for page in range(pages_needed):
        params = {
            "engine": "google", "q": query, "api_key": api_key,
            "num": per_page, "start": page * per_page,
        }

        data, last_error = None, None
        for attempt in range(3):
            try:
                resp = requests.get(SERPAPI_ENDPOINT, params=params, timeout=30)
                resp.raise_for_status()
                data = resp.json()
                if data.get("error"):
                    last_error = data["error"]
                    data = None
                    break
                break
            except requests.exceptions.Timeout:
                last_error = "Read timed out"
                time.sleep(1.5 * (attempt + 1))
            except Exception as e:
                last_error = str(e)
                time.sleep(1.5 * (attempt + 1))

        if data is None:
            st.error(f"SerpApi request failed after retries: {last_error}")
            break

        if total_results_estimate is None:
            total_results_estimate = data.get("search_information", {}).get("total_results")

        organic = data.get("organic_results", [])
        if not organic:
            break

        for item in organic:
            link = item.get("link")
            if not link:
                continue
            results.append({
                "title": item.get("title", ""),
                "website": link,
                "snippet": item.get("snippet", ""),
            })
            if len(results) >= num_results:
                break

        if len(results) >= num_results:
            break

    seen_domains, deduped = set(), []
    for r in results:
        domain = urlparse(r["website"]).netloc
        if domain not in seen_domains:
            seen_domains.add(domain)
            deduped.append(r)

    return deduped, total_results_estimate


# ==================================================================
# ORCHESTRATION (shared by both modes)
# ==================================================================
def process_sites(site_list, crawl_extra_pages, max_workers, progress_callback=None, label_key="Website"):
    """
    site_list: list of either plain URL strings (bulk mode) or dicts with
    'title'/'website'/'snippet' (search mode).
    """
    results = []
    total = len(site_list)
    completed = 0

    def worker(item):
        website = item if isinstance(item, str) else item["website"]
        info = extract_contact_info_from_website(website, crawl_extra_pages=crawl_extra_pages)
        return item, info

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(worker, item): item for item in site_list}
        for future in as_completed(futures):
            item = futures[future]
            website = item if isinstance(item, str) else item["website"]
            try:
                item, info = future.result()
                row = {
                    "Website": website,
                    "Company Name": info["company_name"],
                    "Contact Person": info["contact_person"],
                    "Email": "; ".join(sorted(info["emails"])) if info["emails"] else "",
                    "Phone": "; ".join(sorted(info["phones"])) if info["phones"] else "",
                    "Location": info["address"],
                    "Pages Checked": len(info["pages_checked"]),
                    "Errors": "; ".join(f"{u} ({err})" for u, err in info["errors"]) if info["errors"] else "",
                }
                if isinstance(item, dict):
                    row["Search Title"] = item.get("title", "")
                    row["Snippet"] = item.get("snippet", "")
                results.append(row)
            except Exception as e:
                results.append({"Website": website, "Company Name": "", "Contact Person": "",
                                 "Email": "", "Phone": "", "Location": "",
                                 "Pages Checked": 0, "Errors": str(e)})

            completed += 1
            if progress_callback:
                progress_callback(completed, total, website)

    return results


def create_excel_file(data, sheet_name="Leads"):
    df = pd.DataFrame(data)
    output = BytesIO()
    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        df.to_excel(writer, index=False, sheet_name=sheet_name)
        worksheet = writer.sheets[sheet_name]
        for i, col in enumerate(df.columns):
            max_len = max(df[col].astype(str).map(len).max() if not df.empty else 0, len(col)) + 2
            worksheet.set_column(i, i, min(max_len, 60))
    output.seek(0)
    return output


# ==================================================================
# STREAMLIT UI
# ==================================================================
st.set_page_config(page_title="Lead Finder Toolkit", layout="centered")

st.sidebar.title("Lead Finder Toolkit")
page = st.sidebar.radio("Choose a tool", ["Bulk Website Extractor", "Google Search Lead Finder"])

# ------------------------------------------------------------------
# PAGE 1: Bulk Website Extractor
# ------------------------------------------------------------------
if page == "Bulk Website Extractor":
    st.title("Bulk Website Extractor")
    st.caption(
        "Paste a list of website URLs (one per line). The tool visits each one and "
        "pulls out email, phone number, company name, contact person, and location."
    )

    websites_input = st.text_area(
        "Enter websites",
        value="https://www.espine.in\nhttps://www.venusremedies.com",
        height=150,
    )

    col1, col2 = st.columns(2)
    with col1:
        crawl_extra = st.checkbox("Also check /contact, /about, /team, etc.", value=True, key="bulk_crawl")
    with col2:
        max_workers = st.slider("Concurrent requests", 1, 16, DEFAULT_MAX_WORKERS, key="bulk_workers")

    if st.button("Extract Contact Details", type="primary", key="bulk_btn"):
        if not websites_input.strip():
            st.warning("Please enter at least one website URL.")
        else:
            websites = [u.strip() for u in websites_input.splitlines() if u.strip()]

            progress_bar = st.progress(0)
            status_text = st.empty()

            def progress_callback(completed, total, site):
                progress_bar.progress(completed / total)
                status_text.write(f"Processed {completed}/{total}: {site}")

            with st.spinner("Extracting contact details..."):
                results = process_sites(websites, crawl_extra, max_workers, progress_callback)

            progress_bar.progress(1.0)

            if results:
                df = pd.DataFrame(results)
                found_emails = (df["Email"] != "").sum()
                found_phones = (df["Phone"] != "").sum()

                st.success(
                    f"Done. {len(df)} website(s) processed — "
                    f"{found_emails} with email(s), {found_phones} with phone number(s)."
                )
                st.dataframe(df, use_container_width=True)

                excel_file = create_excel_file(results, sheet_name="Contacts")
                st.download_button(
                    label="Download as Excel",
                    data=excel_file,
                    file_name="bulk_extracted_contacts.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            else:
                st.warning("No results.")

# ------------------------------------------------------------------
# PAGE 2: Google Search Lead Finder
# ------------------------------------------------------------------
else:
    st.title("Google Search Lead Finder")
    st.caption("Search Google by keyword or category, then extract contact info from the websites found.")

    with st.expander("SerpApi settings", expanded=True):
        api_key = st.text_input("SerpApi API key", type="password", help="Get one free at serpapi.com")

        search_mode = st.radio("Search mode", ["Keyword (raw)", "Category + Location"], horizontal=True)

        if search_mode == "Keyword (raw)":
            keyword = st.text_input(
                "Search keyword", value="pharmaceutical companies India",
                help="Type exactly what you'd type into Google.",
            )
            query = keyword.strip()
        else:
            colA, colB = st.columns(2)
            with colA:
                category = st.text_input("Business category", value="pharmaceutical")
            with colB:
                location = st.text_input("Location (optional)", value="India")
            query = f"{category.strip()} companies" + (f" in {location.strip()}" if location.strip() else "")

        num_results = st.slider("Number of websites to fetch", 5, 50, 15, key="search_num")

    col3, col4 = st.columns(2)
    with col3:
        crawl_extra = st.checkbox("Also check /contact, /about, /team, etc.", value=True, key="search_crawl")
    with col4:
        max_workers = st.slider("Concurrent requests", 1, 16, DEFAULT_MAX_WORKERS, key="search_workers")

    if "search_results" not in st.session_state:
        st.session_state.search_results = None
    if "total_estimate" not in st.session_state:
        st.session_state.total_estimate = None

    search_col, extract_col = st.columns(2)

    with search_col:
        if st.button("Search Google", type="secondary"):
            if not api_key:
                st.warning("Please enter your SerpApi API key.")
            elif not query:
                st.warning("Please enter a search keyword or category.")
            else:
                with st.spinner(f"Searching Google for: {query}"):
                    businesses, total_estimate = search_businesses(api_key, query, num_results)
                st.session_state.search_results = businesses
                st.session_state.total_estimate = total_estimate

    if st.session_state.search_results is not None:
        businesses = st.session_state.search_results
        total_estimate = st.session_state.total_estimate

        if total_estimate:
            st.info(
                f"Google reports roughly **{total_estimate:,}** total results for this query. "
                f"Fetched the top **{len(businesses)}** unique website(s) to extract from."
            )
        else:
            st.info(f"Fetched **{len(businesses)}** unique website(s) to extract from.")

        preview_df = pd.DataFrame(businesses)
        if not preview_df.empty:
            st.dataframe(preview_df[["title", "website"]], use_container_width=True)

            # Download the raw search results (titles + URLs + snippets) as-is —
            # no website crawling needed for this, it's just the Google list.
            search_excel = create_excel_file(businesses, sheet_name="Search Results")
            safe_name_dl = re.sub(r'[^a-zA-Z0-9]+', '_', query)[:40]
            st.download_button(
                label="Download Website List as Excel",
                data=search_excel,
                file_name=f"search_results_{safe_name_dl}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key="download_search_list",
            )

        with extract_col:
            extract_clicked = st.button("Extract Contact Info from These Websites", type="primary")

        if extract_clicked:
            if not businesses:
                st.warning("No websites to extract from. Try a different search.")
            else:
                progress_bar = st.progress(0)
                status_text = st.empty()

                def progress_callback(completed, total, site):
                    progress_bar.progress(completed / total)
                    status_text.write(f"Processed {completed}/{total}: {site}")

                with st.spinner("Visiting websites and extracting contact details..."):
                    lead_data = process_sites(
                        businesses, crawl_extra, max_workers, progress_callback
                    )

                progress_bar.progress(1.0)

                df = pd.DataFrame(lead_data)
                found_emails = (df["Email"] != "").sum()
                found_phones = (df["Phone"] != "").sum()

                st.success(
                    f"Done. {len(df)} website(s) processed — "
                    f"{found_emails} with email(s), {found_phones} with phone number(s)."
                )
                st.dataframe(df, use_container_width=True)

                excel_file = create_excel_file(lead_data, sheet_name="Leads")
                safe_name = re.sub(r'[^a-zA-Z0-9]+', '_', query)[:40]
                st.download_button(
                    label="Download Leads as Excel",
                    data=excel_file,
                    file_name=f"leads_{safe_name}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )

st.markdown("---")
st.caption(
    "Note: contact details are pulled from publicly listed info on each website. "
    "Check applicable laws (e.g. CAN-SPAM, GDPR, TCPA) before using this data for outreach."
)
