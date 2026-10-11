"""Candidate track selection and explicit experience requirements."""

import re

CANDIDATE_A_TITLES = (
    'soc analyst', 'soc tier', 'tier 1', 'tier i', 'tier1',
    'associate security analyst', 'security analyst', 'cybersecurity analyst',
    'information security analyst', 'security operations', 'incident response',
    'threat analyst', 'security engineer', 'cybersecurity engineer', 'security specialist',
    'security administrator', 'gsoc analyst',
    'penetration tester', 'pentest', 'vulnerability analyst', 'it security', 'cyber analyst',
    'vulnerability management analyst', 'vulnerability management specialist',
    'security intern', 'security graduate', 'blue team', 'blueteam', 'malware analyst',
    'malware researcher', 'digital forensics', 'dfir', 'grc',
    'governance risk and compliance', 'it risk', 'technology risk', 'cyber risk',
    'security risk', 'vendor risk', 'third party risk', 'third-party risk',
    'risk and compliance', 'regulatory risk', 'information security compliance',
    'cybersecurity compliance', 'security compliance', 'it compliance',
    'technology compliance', 'it audit', 'cyber audit', 'security audit',
    'compliance analyst', 'risk analyst', 'governance analyst', 'compliance auditor',
    'internal auditor', 'it auditor', 'technology auditor', 'audit analyst',
    'privacy analyst', 'privacy administrator', 'policy analyst',
    'information assurance', 'controls analyst', 'it controls analyst',
    'technology controls analyst', 'security assurance analyst',
    'security awareness analyst', 'cybersecurity awareness analyst',
    'risk management coordinator',
    'iam analyst', 'identity and access management', 'identity governance analyst',
    'access management analyst', 'access governance analyst', 'user access analyst',
    'it support', 'information technology support', 'service desk',
    'help desk', 'helpdesk', 'desktop support', 'it technician', 'it specialist',
    'technical support analyst', 'technical support engineer', 'cloud support associate',
    'cloud support engineer', 'network support specialist', 'network support technician',
    'noc analyst', 'network operations center analyst', 'it operations analyst',
    'it analyst', 'system administrator', 'systems administrator',
    'systems operations administrator', 'network administrator', 'it administrator',
    'information technology specialist',
    'noc technician', 'network operations technician', 'data center technician',
    'it audit associate', 'access provisioning analyst',
)
CANDIDATE_A_DESCRIPTION = (
    'soc analyst', 'security operations center', 'siem', 'incident response',
    'threat detection', 'vulnerability', 'penetration test', 'cyber threat',
    'security monitoring', 'malware', 'forensics', 'identity and access management',
    'access governance', 'vulnerability management', 'security awareness',
    'governance', 'risk management',
    'compliance', 'grc', 'audit', 'iso 27001', 'nist csf', 'regulatory',
)
CANDIDATE_B_TITLES = (
    'grc', 'governance risk and compliance', 'it risk', 'technology risk',
    'cyber risk', 'security risk', 'vendor risk', 'third party risk',
    'third-party risk', 'risk and compliance', 'regulatory risk',
    'information security compliance', 'cybersecurity compliance',
    'security compliance', 'it compliance', 'technology compliance', 'it audit',
    'cyber audit', 'security audit', 'compliance analyst', 'risk analyst',
    'governance analyst', 'regulatory compliance analyst', 'compliance auditor',
    'internal auditor', 'it auditor', 'technology auditor', 'audit analyst',
    'privacy analyst', 'privacy and governance', 'policy analyst',
    'information assurance', 'controls analyst', 'it controls analyst',
    'technology controls analyst', 'security assurance analyst',
    'identity governance analyst', 'access governance analyst',
    'risk management coordinator',
    'vrm analyst', 'trm analyst', 'tprm analyst', 'risk/compliance analyst',
    'information security analyst',
)
CANDIDATE_B_DESCRIPTION = (
    'grc', 'governance, risk', 'risk and compliance', 'risk management',
    'compliance analyst', 'compliance program', 'iso 27001', 'nist csf',
    'sox compliance', 'hipaa compliance', 'audit', 'third-party risk', 'vendor risk',
    'identity governance', 'access governance', 'control testing',
)

# These phrases use risk/compliance terminology but belong to finance, trade,
# legal, safety, or clerical tracks rather than cybersecurity/technology GRC.
NON_CYBER_TRACKS = (
    'credit risk', 'market risk', 'liquidity risk', 'investment risk',
    'counterparty risk', 'margin and collateral', 'trade compliance',
    'export compliance', 'import compliance', 'tax compliance',
    'environmental compliance', 'health and safety', 'food safety',
    'regulatory affairs', 'administrative assistant', 'executive assistant',
)

TECH_SECURITY_SIGNALS = (
    'information security', 'cybersecurity', 'cyber security', 'it risk',
    'technology risk', 'security control', 'access control',
    'identity and access', 'identity governance', 'access governance',
    'security audit', 'vulnerability management', 'security awareness',
    'soc 2', 'nist', 'iso 27001', 'siem',
)


def _contains_phrase(text, phrase):
    """Match complete words so `nist` cannot match `administrative`."""
    return bool(re.search(r'(?<!\w)' + re.escape(phrase) + r'(?!\w)', text))


def candidate_matches_track(job, candidate):
    title = (job.get('jobTitle') or '').lower()
    description = (job.get('jobDescription') or '')[:1500].lower()
    if any(_contains_phrase(title, term) for term in NON_CYBER_TRACKS):
        return False
    titles, terms = ((CANDIDATE_B_TITLES, CANDIDATE_B_DESCRIPTION) if candidate == 'candidate_b'
                     else (CANDIDATE_A_TITLES, CANDIDATE_A_DESCRIPTION))
    if any(_contains_phrase(title, term) for term in titles):
        return True
    # Generic titles need two independent domain signals. One incidental use of
    # "compliance" or "audit" is not enough to make a role cybersecurity/GRC.
    description_hits = {term for term in terms if _contains_phrase(description, term)}
    security_context = any(_contains_phrase(description, term) for term in TECH_SECURITY_SIGNALS)
    return 'grc' in description_hits or (security_context and len(description_hits) >= 2)


def required_experience_years(text):
    """Ignore company age/preferred experience and use the lower end of a range.

    Ambiguous qualifications are left to full job/candidate scoring.
    """
    years = []
    normalized = (text or '').lower().replace('\\', '')
    # Normalize HR/legal notation such as "Two (2) years".
    normalized = re.sub(
        r'\b(?:zero|one|two|three|four|five|six|seven|eight|nine|ten)\s*\(\s*(\d+)\s*\)',
        r'\1', normalized,
    )
    for clause in re.split(r'[\n.;!?]', normalized):
        for match in re.finditer(r'\b(\d+)\s*(?:(?:[-–]|to)\s*\d+\s*)?(?:\+|or more)?\s*years?\b', clause):
            before = clause[max(0, match.start() - 60):match.start()]
            after = clause[match.end():match.end() + 90]
            context = before + match.group() + after
            if re.search(r'\b(preferred|desirable|ideally|nice to have|a plus)\b', context):
                continue
            # A number of years is not necessarily professional experience.
            if not (re.match(r"(?:\s|['’]|of\b|relevant\b|professional\b|hands-on\b|work\b|direct\b|related\b|it\b|security\b|cybersecurity\b|in\b|industry\b)*experience\b", after)
                    or re.search(r'\bexperience\s*[:\-]?\s*$', before)):
                continue
            years.append(int(match.group(1)))
    return min(years) if years else None
