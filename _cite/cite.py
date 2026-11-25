"""
cite process to convert sources and metasources into full citations
"""

import traceback
from importlib import import_module
from pathlib import Path
from dotenv import load_dotenv
from util import *


# load environment variables
load_dotenv()


# a publication older than this many years is pruned unless it has at least
# PRUNE_BELOW_CITATIONS citations. set PRUNE_OLDER_THAN_YEARS to 0 to disable.
PRUNE_OLDER_THAN_YEARS = 10
PRUNE_BELOW_CITATIONS = 20

# publication types that are exempt from the citation threshold, because the
# citation sources do not index them and would report none for all of them
PRUNE_EXEMPT_TYPES = ["patent"]


# save errors/warnings for reporting at end
errors = []
warnings = []

# output citations file
output_file = "_data/citations.yaml"


log()

log("Compiling sources")

# compiled list of sources
sources = []

# in-order list of plugins to run
plugins = ["google-scholar", "pubmed", "orcid", "sources"]

# loop through plugins
for plugin in plugins:
    # convert into path object
    plugin = Path(f"plugins/{plugin}.py")

    log(f"Running {plugin.stem} plugin")

    # get all data files to process with current plugin
    files = Path.cwd().glob(f"_data/{plugin.stem}*.*")
    files = list(filter(lambda p: p.suffix in [".yaml", ".yml", ".json"], files))

    log(f"Found {len(files)} {plugin.stem}* data file(s)", indent=1)

    # loop through data files
    for file in files:
        log(f"Processing data file {file.name}", indent=1)

        # load data from file
        try:
            data = load_data(file)
            # check if file in correct format
            if not list_of_dicts(data):
                raise Exception(f"{file.name} data file not a list of dicts")
        except Exception as e:
            log(e, indent=2, level="ERROR")
            errors.append(e)
            continue

        # loop through data entries
        for index, entry in enumerate(data):
            log(f"Processing entry {index + 1} of {len(data)}, {label(entry)}", level=2)

            # run plugin on data entry to expand into multiple sources
            try:
                expanded = import_module(f"plugins.{plugin.stem}").main(entry)
                # check that plugin returned correct format
                if not list_of_dicts(expanded):
                    raise Exception(f"{plugin.stem} plugin didn't return list of dicts")
            # catch any plugin error
            except Exception as e:
                # log detailed pre-formatted/colored trace
                print(traceback.format_exc())
                # log high-level error
                log(e, indent=3, level="ERROR")
                errors.append(e)
                continue

            # loop through sources
            for source in expanded:
                if plugin.stem != "sources":
                    log(label(source), level=3)

                # include meta info about source
                source["plugin"] = plugin.name
                source["file"] = file.name

                # add source to compiled list
                sources.append(source)

            if plugin.stem != "sources":
                log(f"{len(expanded)} source(s)", indent=3)


log("Merging sources by id")

# merge sources with matching (non-blank) ids
for a in range(0, len(sources)):
    a_id = get_safe(sources, f"{a}.id", "")
    if not a_id:
        continue
    for b in range(a + 1, len(sources)):
        b_id = get_safe(sources, f"{b}.id", "")
        if b_id == a_id:
            log(f"Found duplicate {b_id}", indent=2)
            sources[a].update(sources[b])
            sources[b] = {}
sources = [entry for entry in sources if entry]


log(f"{len(sources)} total source(s) to cite")


log()

log("Generating citations")

# list of new citations
citations = []


# titles explicitly flagged for removal. a source flagged with remove and no id
# cannot be matched until manubot has resolved titles, so collect them here and
# filter after the citations are generated.
removed_titles = {
    normalize_title(get_safe(source, "title", ""))
    for source in sources
    if get_safe(source, "remove", False) == True
    and get_safe(source, "title", "")
    and not get_safe(source, "id", "")
}

# sources flagged with merge patch fields onto a citation that a metasource
# already supplies, matched by title. sources that carry an id are merged by id
# above; this covers the ones that do not, where there is nothing else to match
# on. the flagged source is not emitted as a citation of its own.
merged_by_title = {}
for source in sources:
    if get_safe(source, "merge", False) != True:
        continue
    key = normalize_title(get_safe(source, "title", ""))
    if not key:
        errors.append("Source flagged with merge has no title to match on")
        continue
    merged_by_title[key] = {
        field: value
        for field, value in source.items()
        if field not in ("merge", "title", "plugin", "file")
    }


# loop through compiled sources
for index, source in enumerate(sources):
    log(f"Processing source {index + 1} of {len(sources)}, {label(source)}")

    # if explicitly flagged, remove/ignore entry
    if get_safe(source, "remove", False) == True:
        continue

    # if flagged for merging, it only patches another citation
    if get_safe(source, "merge", False) == True:
        continue

    # new citation data for source
    citation = {}

    # source id
    _id = get_safe(source, "id", "").strip()

    # manubot doesn't work without an id
    if _id:
        log("Using Manubot to generate citation", indent=1)

        try:
            # run manubot and set citation
            citation = cite_with_manubot(_id)

        # if manubot cannot cite source
        except Exception as e:
            plugin = get_safe(source, "plugin", "")
            file = get_safe(source, "file", "")
            # if regular source (id entered by user), throw error
            if plugin == "sources.py":
                log(e, indent=3, level="ERROR")
                errors.append(f"Manubot could not generate citation for source {_id}")
            # otherwise, if from metasource (id retrieved from some third-party api), just warn
            else:
                log(e, indent=3, level="WARNING")
                warnings.append(
                    f"Manubot could not generate citation for source {_id} (from {file} with {plugin})"
                )
                # discard source from citations
                continue

    # preserve fields from input source, overriding existing fields
    citation.update(source)

    # ensure date in proper format for correct date sorting
    if get_safe(citation, "date", ""):
        citation["date"] = format_date(get_safe(citation, "date", ""))

    # add new citation to list
    citations.append(citation)


log()


# drop citations whose titles were flagged for removal. this covers works that
# a metasource supplies without a citable id, which cannot be matched by id.
if removed_titles:
    kept = []
    for citation in citations:
        if normalize_title(get_safe(citation, "title", "")) in removed_titles:
            log(f"Removing flagged citation {get_safe(citation, 'title', '')[:60]}", indent=1)
        else:
            kept.append(citation)
    if len(kept) != len(citations):
        log(f"Removed {len(citations) - len(kept)} flagged citation(s)", indent=1)
    citations = kept


# a work can show up more than once: orcid may hold a metadata-only copy
# alongside one with a doi, and the same work can arrive from several plugins.
# prefer the cited copy, then the one carrying the most detail.
def _merit(citation):
    return (
        1 if get_safe(citation, "id", "") else 0,
        sum(1 for key in ("authors", "publisher", "date", "link") if citation.get(key)),
    )


deduped = {}
for citation in citations:
    key = normalize_title(get_safe(citation, "title", ""))
    # keep untitled entries as-is, they cannot be compared
    if not key:
        deduped[id(citation)] = citation
        continue
    if key not in deduped or _merit(citation) > _merit(deduped[key]):
        if key in deduped:
            log(f"Discarding duplicate of {key[:60]}", indent=1, level="WARNING")
        deduped[key] = citation

removed = len(citations) - len(deduped)
if removed:
    log(f"Removed {removed} duplicate citation(s)", indent=1)
citations = list(deduped.values())


log()


# apply fields from sources flagged with merge
if merged_by_title:
    unmatched = set(merged_by_title)
    for citation in citations:
        key = normalize_title(get_safe(citation, "title", ""))
        if key in merged_by_title:
            citation.update(merged_by_title[key])
            unmatched.discard(key)
            log(f"Merged fields into {get_safe(citation, 'title', '')[:45]}", indent=1)
    for key in unmatched:
        warnings.append(f"Source flagged with merge matched no citation: {key}")


log()


# point arxiv works at arxiv.org. manubot gives an arxiv doi a shortdoi link
# such as https://doi.org/hcf3w2, which hides where it actually goes.
for citation in citations:
    link = arxiv_url(citation)
    if link:
        citation["link"] = link
        log(f"Linking {get_safe(citation, 'title', '')[:45]} to {link}", indent=1)


log()

log("Filtering citations")

# drop entries with no publisher, and older entries that were never taken up.
# the age cutoff is calendar-exact rather than 365 * n days.
today = date.today()
try:
    cutoff = today.replace(year=today.year - PRUNE_OLDER_THAN_YEARS)
except ValueError:
    # today is 29 february and the cutoff year is not a leap year
    cutoff = today.replace(year=today.year - PRUNE_OLDER_THAN_YEARS, day=28)


def keep(citation):
    title = get_safe(citation, "title", "") or ""

    # must say where it was published
    if not (get_safe(citation, "publisher", "") or "").strip():
        log(f"Dropping (no publisher) {title[:45]}", indent=1)
        return False

    if not PRUNE_OLDER_THAN_YEARS:
        return True

    # some types are not indexed for citations at all
    if get_safe(citation, "type", "") in PRUNE_EXEMPT_TYPES:
        return True

    # only older work is subject to the citation threshold
    published = get_safe(citation, "date", "")
    try:
        published = datetime.strptime(published, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        # no usable date, so age cannot be judged. keep it.
        return True
    if published >= cutoff:
        return True

    # how many times has it been cited
    _id = str(get_safe(citation, "id", "") or "")
    doi = _id[4:] if _id.startswith("doi:") else (_id if _id.startswith("10.") else None)
    count, per_source = citation_count(doi, title)
    detail = ", ".join(
        f"{source} {value if value is not None else '?'}"
        for source, value in per_source.items()
    )
    if count is None:
        # no source knows the work. treated as uncited, which is the point of
        # the filter, but say so rather than letting it disappear quietly.
        log(
            f"No citation count for {title[:45]} ({detail}), treating as 0",
            indent=1,
            level="WARNING",
        )
        count = 0
    if count < PRUNE_BELOW_CITATIONS:
        log(
            f"Dropping ({published.year}, {count} citations = {detail}) {title[:34]}",
            indent=1,
        )
        return False

    return True


before = len(citations)
citations = [citation for citation in citations if keep(citation)]
log(f"Kept {len(citations)} of {before} citation(s)", indent=1)


log()

log("Saving updated citations")


# save new citations
try:
    save_data(output_file, citations)
except Exception as e:
    log(e, level="ERROR")
    errors.append(e)


log()


# exit at end, so user can see all errors/warnings in one run
if len(warnings):
    log(f"{len(warnings)} warning(s) occurred above", level="WARNING")
    for warning in warnings:
        log(warning, indent=1, level="WARNING")

if len(errors):
    log(f"{len(errors)} error(s) occurred above", level="ERROR")
    for error in errors:
        log(error, indent=1, level="ERROR")
    log()
    exit(1)

else:
    log("All done!", level="SUCCESS")

log()
