"""Conservative eligibility decisions: reject explicit conflicts, review uncertainty."""
import re
from datetime import datetime, timedelta, timezone

US_STATES = 'Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|Florida|Georgia|Hawaii|Idaho|Illinois|Indiana|Iowa|Kansas|Kentucky|Louisiana|Maine|Maryland|Massachusetts|Michigan|Minnesota|Mississippi|Missouri|Montana|Nebraska|Nevada|New Hampshire|New Jersey|New Mexico|New York|North Carolina|North Dakota|Ohio|Oklahoma|Oregon|Pennsylvania|Rhode Island|South Carolina|South Dakota|Tennessee|Texas|Utah|Vermont|Virginia|Washington|West Virginia|Wisconsin|Wyoming|District of Columbia'
US_CODES = 'AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC'.split()
ALLOWED = re.compile(r'\b(?:united states|usa|u\.s\.?|india|bengaluru|bangalore|hyderabad|mumbai|pune|chennai|delhi|noida|gurgaon|gurugram|kolkata|ahmedabad|jaipur|kochi|thiruvananthapuram|chandigarh|' + US_STATES + r')\b', re.I)
FOREIGN = re.compile(r'\b(?:uk|u\.k\.?|united kingdom|england|london|canada|toronto|vancouver|australia|sydney|germany|berlin|france|paris|ireland|dublin|netherlands|singapore|emea|europe|eu only|new zealand|south africa)\b', re.I)
CLEARANCE = re.compile(r'\b(?:security clearance|secret clearance|clearance|us citizen(?:ship)?|u\.s\. citizen(?:ship)?|united states citizen(?:ship)?|green card|permanent resident|citizenship|top secret|ts/sci|public trust)\b', re.I)


def location_decision(location, description=''):
    loc = (location or '').strip()
    # LinkedIn sometimes supplies a metro name without city/state/country.
    # Use exact, unambiguous labels; unknown cities still require review.
    us_metro = loc.casefold().replace('–', '-').replace('—', '-') in {
        'albuquerque-santa fe metropolitan area',
        'dallas-fort worth metroplex',
        'san francisco bay area',
        'greater seattle area',
        'greater boston',
        'greater chicago area',
    }
    allowed = bool(us_metro or ALLOWED.search(loc) or re.search(r',\s*(?:' + '|'.join(US_CODES) + r')\b', loc) or re.search(r'\bUS\b', loc))
    foreign = bool(FOREIGN.search(loc))
    if foreign and not allowed:
        return 'rejected', 'Location restricts work outside US/India: ' + loc
    if foreign and allowed:
        return 'needs_review', 'Multiple countries listed; confirm an eligible work location: ' + loc
    # Trust an explicit location header over a bad aggregator label (for example,
    # "Ontario, CA" paired with "Location: Canada" in the actual posting).
    for raw_line in (description or '')[:1500].splitlines():
        line = re.sub(r'^[\s*#_-]+|[\s*#_-]+$', '', raw_line).strip()
        header = re.match(r'(?i)^(?:job\s+)?location\s*:\s*(.+)$', line)
        if not header:
            continue
        stated = header.group(1).strip()
        if FOREIGN.search(stated) and not ALLOWED.search(stated):
            return 'rejected', 'Posting explicitly states a location outside US/India: ' + stated

    # Explicit local-only restrictions need confirmation even when the city is in
    # the US; a generic country on a resume does not prove local residency.
    restrictions = re.findall(
        r'[^\n.!?]*(?:must (?:currently )?(?:be (?:based|located|local)|reside|live)|'
        r'(?:remote|residents) (?:in|within)|local to|only (?:in|within)|'
        r'(?:role|position|job|candidate|applicant|employee|you)\b[^\n.!?]{0,45}\bbased in|'
        r'(?:willingness|ability|required)[^\n.!?]{0,45}\bcommute to)[^\n.!?]*',
        description, re.I,
    )
    for clause in restrictions:
        if re.search(r'\b(prefer|preferred|preference|ideally|optional)\b', clause, re.I) \
                and not re.search(r'\b(must|required|only|will be)\b', clause, re.I):
            continue
        if FOREIGN.search(clause) and not ALLOWED.search(clause):
            return 'rejected', 'Work-location restriction: ' + clause.strip()
        country_only = re.search(r'\b(?:united states|usa|u\.s\.?|india)\b', clause, re.I)
        specific_us_place = re.search(r'\b(?:' + US_STATES + r')\b|,\s*(?:' + '|'.join(US_CODES) + r')\b', clause, re.I)
        if country_only and not specific_us_place:
            continue
        if ALLOWED.search(clause) or re.search(r',\s*(?:' + '|'.join(US_CODES) + r')\b', clause):
            return 'needs_review', 'Confirm explicit local/residency requirement: ' + clause.strip()
    if allowed:
        return 'eligible', 'US/India location stated'
    if re.search(r'\b(worldwide|anywhere in the world|globally)\b', loc, re.I):
        return 'eligible', 'Worldwide remote location stated'
    return 'needs_review', 'Confirm permitted work country; location is unspecified or ambiguous: ' + (loc or 'Unknown')


def clearance_decision(description):
    ambiguous = []
    # Preserve U.S. abbreviations while splitting sentences and contrasting clauses.
    normalized = re.sub(r'\bu\.s\.', 'US', description or '', flags=re.I)
    normalized = re.sub(
        r'\b(?:minimum\s+)?(?:security\s+)?clearance(?:\s+level)?\s*'
        r'(?:(?:required|must\s+be\s+able\s+to\s+obtain)\s*)?:\s*'
        r'(?:none|not\s+required|not\s+applicable)\b', '', normalized, flags=re.I)
    for clause in re.split(r'[\n;.!?]|\bbut\b|\bhowever\b', normalized, flags=re.I):
        if not CLEARANCE.search(clause):
            continue
        # Remove only explicitly negated requirements, then inspect remaining requirements.
        remaining = re.sub(r'\b(?:no|without)\s+(?:active\s+)?' + CLEARANCE.pattern + r'(?:\s+(?:is\s+)?(?:required|needed|necessary))?', '', clause, flags=re.I)
        remaining = re.sub(r'\b(?:do not need|does not require|not required to (?:have|hold))\s+(?:a |an )?' + CLEARANCE.pattern, '', remaining, flags=re.I)
        remaining = re.sub(CLEARANCE.pattern + r'\s+(?:is\s+)?(?:not required|not necessary|not needed)', '', remaining, flags=re.I)
        if not CLEARANCE.search(remaining):
            continue
        if re.search(r'\b(regardless of|without regard to|non-discrimination|not discriminate|equal opportunity|protected by applicable)\b', remaining, re.I):
            continue
        if re.search(r'\b(preferred|desirable|a plus|optional)\b', remaining, re.I) and not re.search(r'\b(must|required|mandatory)\b', remaining, re.I):
            continue
        if re.search(r'\b(must|required|requires|requirement|mandatory|shall|only|active|ability to obtain|eligible to obtain)\b', remaining, re.I):
            return 'rejected', 'Explicit clearance/citizenship requirement: ' + clause.strip()
        ambiguous.append(clause.strip())
    if ambiguous:
        return 'needs_review', 'Confirm clearance/citizenship requirement: ' + '; '.join(ambiguous)
    return 'eligible', 'No explicit clearance/citizenship conflict'


def eligibility_decision(job):
    decisions = [location_decision(job.get('location'), job.get('jobDescription', '')),
                 clearance_decision((job.get('jobTitle') or '') + '\n' + (job.get('jobDescription') or ''))]
    for state in ('rejected', 'needs_review'):
        reasons = [reason for outcome, reason in decisions if outcome == state]
        if reasons:
            return state, '; '.join(reasons)
    return 'eligible', 'Location and clearance checks passed'


def posted_datetime(job):
    for value in (job.get('publishedAt'), job.get('postedTime')):
        try:
            stamp = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            return stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp.astimezone(timezone.utc)
        except (ValueError, TypeError):
            pass
    return None


def outside_discovery_window(job, now=None):
    stamp = posted_datetime(job)
    if stamp is None:
        return False
    now = now or datetime.now(timezone.utc)
    # Date-only postings have day precision, so include the entire seventh day.
    return stamp.date() < (now - timedelta(days=7)).date()


def freshness_key(job):
    stamp = posted_datetime(job)
    return stamp.timestamp() if stamp else 0
