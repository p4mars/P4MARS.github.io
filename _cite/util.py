"""
utility functions for cite process and plugins
"""

import re
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from urllib.parse import quote
import subprocess
import json
from time import sleep
import yaml
from yaml.loader import SafeLoader
from pathlib import Path
from datetime import date, datetime
from rich import print
from diskcache import Cache


# cache for time-consuming network requests
cache = Cache("./_cite/.cache")


# clear expired items from cache
cache.expire()


def log_cache(func):
    """
    decorator to use around memoized function to log if cached or or not
    """

    def wrap(*args):
        key = func.__cache_key__(*args)
        if key in cache:
            log(" (from cache)", level="INFO", newline=False)
        return func(*args)

    return wrap


def log(message="\n--------------------\n", indent=0, level="", newline=True):
    """
    log to terminal, color determined by indent and level
    """

    colors = {
        0: "[orange1]",
        1: "[salmon1]",
        2: "[violet]",
        3: "[sky_blue1]",
        "ERROR": "[#F43F5E]",
        "WARNING": "[#EAB308]",
        "SUCCESS": "[#10B981]",
        "INFO": "[grey70]",
    }
    prefixes = {
        "ERROR": "🚫 ERROR: ",
        "WARNING": "⚠️ WARNING: ",
    }
    color = get_safe(colors, level, "") or get_safe(colors, indent, "") or "[white]"
    prefix = get_safe(prefixes, level, "")
    if newline:
        print()
    print(indent * "    " + color + prefix + str(message) + "[/]", end="", flush=True)


def label(entry):
    """
    get "label" of dict entry (for logging purposes)
    """

    return str(list(entry.keys())[0]) + ": " + str(list(entry.values())[0])


def get_safe(item, path, default=None):
    """
    safely access value in nested lists/dicts
    """

    for part in str(path).split("."):
        try:
            part = int(part)
        except ValueError:
            part = part
        try:
            item = item[part]
        except (KeyError, IndexError, AttributeError, TypeError):
            return default
    return item


def index_of(_list, value, fallback=float("inf")):
    """
    index of, with fallback
    """

    try:
        return _list.index(value)
    except ValueError:
        return fallback


def _api_get(url, retries=4):
    """
    fetch json, backing off when a shared rate limit pool pushes back
    """

    for attempt in range(retries):
        try:
            request = Request(url, headers={"User-Agent": "lab-website-template"})
            with urlopen(request, timeout=30) as response:
                return json.load(response)
        except HTTPError as e:
            if e.code not in (429, 503) or attempt == retries - 1:
                return None
            sleep(2 * (attempt + 1))
        except Exception:
            return None
    return None


@cache.memoize(name="openalex", expire=30 * (60 * 60 * 24))
def openalex_count(doi, title):
    """
    times cited according to openalex, or None if the work is not found
    """

    if doi:
        work = _api_get(f"https://api.openalex.org/works/doi:{quote(doi, safe='')}")
        if isinstance(work, dict) and work.get("cited_by_count") is not None:
            return work["cited_by_count"]

    if title:
        found = _api_get(
            "https://api.openalex.org/works"
            f"?filter=title.search:{quote(title, safe='')}&per-page=1"
        )
        result = (get_safe(found, "results", []) or [None])[0]
        if result and normalize_title(result.get("title")) == normalize_title(title):
            return result.get("cited_by_count")

    return None


@cache.memoize(name="semantic-scholar", expire=30 * (60 * 60 * 24))
def semantic_scholar_count(doi, title):
    """
    times cited according to semantic scholar, or None if the work is not found
    """

    api = "https://api.semanticscholar.org/graph/v1/paper"

    if doi:
        paper = _api_get(f"{api}/DOI:{quote(doi, safe='')}?fields=citationCount")
        if isinstance(paper, dict) and paper.get("citationCount") is not None:
            return paper["citationCount"]

    if title:
        found = _api_get(
            f"{api}/search/match?query={quote(title, safe='')}"
            "&fields=title,citationCount"
        )
        match = (get_safe(found, "data", []) or [None])[0]
        if match and normalize_title(match.get("title")) == normalize_title(title):
            return match.get("citationCount")

    return None


def citation_count(doi, title):
    """
    times cited, averaged over the sources that know the work.

    the two disagree routinely, by a few citations on small counts and by tens
    on large ones, because they index different corpora. averaging keeps one
    source's blind spot from deciding whether a work is kept, and returns a
    count from whichever source has it when only one does.

    returns (average, {source: count}) with average None if neither knows it.
    """

    counts = {
        "openalex": openalex_count(doi, title),
        "semantic-scholar": semantic_scholar_count(doi, title),
    }
    known = [count for count in counts.values() if count is not None]
    average = round(sum(known) / len(known)) if known else None
    return average, counts


def arxiv_url(citation):
    """
    arxiv.org link for a work that is on arxiv, else None
    """

    _id = str(get_safe(citation, "id", "") or "")
    # an arxiv doi, e.g. 10.48550/arXiv.2606.05975
    match = re.search(r"10\.48550/arxiv\.([0-9]{4}\.[0-9]{4,5}(?:v[0-9]+)?)", _id, re.I)
    if not match:
        # manubot and orcid often only note it in the publisher field,
        # e.g. "arXiv preprint arXiv:1506.04904"
        match = re.search(
            r"arxiv[:\s]\s*([0-9]{4}\.[0-9]{4,5}(?:v[0-9]+)?)",
            str(get_safe(citation, "publisher", "") or ""),
            re.I,
        )
    if not match:
        return None
    return f"https://arxiv.org/abs/{match.group(1)}"


def normalize_title(title):
    """
    normalize a title for comparison, ignoring case, punctuation and spacing
    """

    return re.sub(r"\W+", " ", (title or "").lower()).strip()


def list_of_dicts(data):
    """
    check if data is list of dicts
    """

    return isinstance(data, list) and all(isinstance(entry, dict) for entry in data)


def format_date(_date):
    """
    format date as YYYY-MM-DD, or no date if malformed
    """

    if isinstance(_date, int):
        return datetime.fromtimestamp(_date // 1000.0).strftime("%Y-%m-%d")
    if isinstance(_date, (date, datetime)):
        return _date.strftime("%Y-%m-%d")
    try:
        return datetime.strptime(_date, "%Y-%m-%d").strftime("%Y-%m-%d")
    except Exception:
        return ""


def load_data(path):
    """
    read data from yaml or json file
    """

    # convert to path object
    path = Path(path)

    # check if file exists
    if not path.is_file():
        raise Exception("Can't find file")

    # try to open file
    try:
        file = open(path, encoding="utf8")
    except Exception as e:
        raise Exception(e or "Can't open file")

    # try to parse as yaml
    try:
        with file:
            data = yaml.load(file, Loader=SafeLoader)
    except Exception:
        raise Exception("Can't parse file. Make sure it's valid YAML.")

    # if no errors, return data
    return data


def save_data(path, data):
    """
    write data to yaml file
    """

    # convert to path object
    path = Path(path)

    # try to open file
    try:
        file = open(path, mode="w")
    except Exception:
        raise Exception("Can't open file for writing")

    # prevent yaml anchors/aliases (pointers)
    yaml.Dumper.ignore_aliases = lambda *args: True

    # try to save data as yaml
    try:
        with file:
            yaml.dump(data, file, default_flow_style=False, sort_keys=False)
    except Exception:
        raise Exception("Can't save YAML to file")

    # write warning note to top of file
    note = "# DO NOT EDIT, GENERATED AUTOMATICALLY"
    try:
        with open(path, "r") as file:
            data = file.read()
        with open(path, "w") as file:
            file.write(f"{note}\n\n{data}")
    except Exception:
        raise Exception("Can't write to file")


@log_cache
@cache.memoize(name="manubot", expire=90 * (60 * 60 * 24))
def cite_with_manubot(_id):
    """
    generate citation data for source id with Manubot
    """

    # run manubot
    try:
        commands = ["manubot", "cite", _id, "--log-level=WARNING"]
        output = subprocess.Popen(commands, stdout=subprocess.PIPE).communicate()
    except Exception as e:
        log(e, indent=3)
        raise Exception("Manubot could not generate citation")

    # parse results as json
    try:
        manubot = json.loads(output[0])[0]
    except Exception:
        raise Exception("Couldn't parse Manubot response")

    # new citation with only needed info
    citation = {}

    # original id
    citation["id"] = _id

    # title
    citation["title"] = get_safe(manubot, "title", "").strip()

    # authors
    citation["authors"] = []
    for author in get_safe(manubot, "author", {}):
        given = get_safe(author, "given", "").strip()
        family = get_safe(author, "family", "").strip()
        if given or family:
            citation["authors"].append(" ".join([given, family]))

    # publisher
    container = get_safe(manubot, "container-title", "").strip()
    collection = get_safe(manubot, "collection-title", "").strip()
    publisher = get_safe(manubot, "publisher", "").strip()
    citation["publisher"] = container or publisher or collection or ""

    # extract date part
    def date_part(citation, index):
        try:
            return citation["issued"]["date-parts"][0][index]
        except (KeyError, IndexError, TypeError):
            return ""

    # date
    year = date_part(manubot, 0)
    if year:
        # fallbacks for month and day
        month = date_part(manubot, 1) or "1"
        day = date_part(manubot, 2) or "1"
        citation["date"] = format_date(f"{year}-{month}-{day}")
    else:
        # if no year, consider date missing data
        citation["date"] = ""

    # link
    citation["link"] = get_safe(manubot, "URL", "").strip()

    # return citation data
    return citation
