"""Conservative, versioned content rules, independent of acquisition dates."""
import re
from datetime import date

CHANNEL = "UCAIZ314ffmfM4PynCR8vElw"
RULE_VERSION = "2"
DEFAULT_START = "2026-10-26"


def classify(metadata, sources, previous=None):
    title = metadata.get("title", "").casefold()
    if metadata.get("channel_id") != CHANNEL:
        return "review", "Channel identity is unknown or differs from the DLS channel"
    for label, pattern in [
        ("Mystery Crate", r"\bmystery crate\b"),
        ("Pitch Clock", r"\bpitch clock\b"),
        ("MMA/UFC watchalong", r"\bmma hangout\b|\bufc\b.*\bwatchalong\b"),
        ("Football America", r"\bfootball america\b"),
        ("Canes Insight", r"\bcanes insight\b"),
        ("Suey nomination compilation", r"\bsuey\b.*\b(nominations?|nominees|compilation|awards?)\b"),
    ]:
        if re.search(pattern, title):
            return "skip", f"Identified excluded series: {label}"
    # Prior positive evidence survives a descriptive retitle; conflicting series
    # evidence above is deliberately evaluated first and remains auditable.
    if previous and previous[0] == "daily_show":
        return "daily_show", "Previously established regular daily show; retained through title changes"
    completed = metadata.get("live_status") == "was_live"
    regular = (re.search(r"\blive at 9(?:[:]00)?\s*am\s*et\b", title)
               and "dan le batard show" in title) or "dls full show" in title
    if completed and regular and "live" in sources:
        return "daily_show", "Completed livestream + regular daily-show branding + Live discovery"
    if "videos" in sources and re.search(r"\b(?:in the club|the club)\b", title):
        return "the_club", "DLS channel Videos upload explicitly branded The Club (no duration exclusion)"
    # These confirmed programs require branding, not incidental mentions.
    # A leading name must end or introduce an episode with punctuation; a
    # trailing Alley Oop label is the channel's observed series suffix.
    for label, pattern in [
        ("Because Miami", r"^\s*#?because\s*miami\b(?=\s*(?:[:|–—-]|$))"),
        ("The Hockey Show", r"^\s*the hockey show\b(?=\s*(?:[:|–—-]|$))"),
        ("MORALLY ABHORRENT", r"^\s*morally abhorrent\b(?=\s*(?:[:|–—-]|$))"),
        ("The Alley Oop (separate future acquisition)", r"^\s*(?:the\s+)?alley oop\b(?=\s*(?:[:|–—-]|$))|\|\s*(?:the\s+)?alley oop\s*$"),
    ]:
        if re.search(pattern, title):
            return "skip", f"Identified excluded series: {label}"
    if re.search(r"\breaction\b|\bwatchalong\b|\bbonus\b|\bexclusive\b|\binterview\b|\bsit down\b", title):
        return "review", "Reaction, interview, bonus, or special-production evidence requires review"
    # Duration is corroboration, never the sole basis for a clip exclusion.
    clip_marker = re.search(r"\b(?:clip|excerpt|from (?:today.s|the) show)\b|\|\s*dls\s*$", title)
    # A topical sports discussion posed as a question is corroborating excerpt
    # evidence; extended uploads and interview/special markers stay in review.
    sports_topic = re.search(r"\b(?:nfl|nba|dolphins|heat|hurricanes|jets|college football|acc|jalen hurts|jayson tatum|jaylen brown)\b", title)
    topical_segment = sports_topic and "?" in title
    duration = metadata.get("duration")
    if ("videos" in sources and not metadata.get("was_live")
            and isinstance(duration, (int, float)) and 0 < duration <= 1200 and (clip_marker or topical_segment)):
        return "skip", "DLS Videos upload + excerpt/segment branding or topical sports question + short runtime; no special-production marker"
    return "review", "Insufficient evidence to distinguish ordinary excerpt from original production"


def publication(metadata):
    """Release date takes precedence; never parse dates out of titles."""
    for field in ("release_date", "upload_date"):
        value = metadata.get(field)
        if isinstance(value, str):
            try:
                return date.fromisoformat(value).isoformat(), field
            except ValueError:
                pass
    return None, None


def eligibility(metadata, category, decision, start=DEFAULT_START, end=None):
    published, _ = publication(metadata)
    if decision in ("reject", "skip"):
        return "blocked", "Manual decision excludes acquisition"
    if decision == "approve":
        return "eligible", "Explicit approval overrides automatic content and date boundaries; acquisition readiness unverified"
    if category not in ("daily_show", "the_club"):
        return "not_selected", "Content is skipped or requires review"
    if not published:
        return "date_unknown", "Selected content lacks a reliable source publication date"
    if published < start or (end and published > end):
        return "outside_range", "Selected content is outside the configured automatic acquisition range"
    return "eligible", "Selected content falls within the automatic acquisition range; acquisition readiness unverified"
