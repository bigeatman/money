"""Conservative DART-report text normalization; original bytes never change."""
import re

from .dart_client import DartError, xml_root

_LABEL = re.compile(r"<([ \t]*[가-힣ㄱ-ㅎㅏ-ㅣ0-9][^<>\r\n]*?)>")
_PROTECTED = re.compile(r"(<!--[\s\S]*?-->|<!\[CDATA\[[\s\S]*?\]\]>)")
_BARE_AMP = re.compile(r"&(?!(?:[A-Za-z][A-Za-z0-9]*|#[0-9]+|#x[0-9A-Fa-f]+);)")


def report_root(raw, decoded):
    """Parse XML, repairing literal labels and bare ampersands in DART reports.

    Some official reports contain `<종속기업>` in a TD as literal prose. This
    is not well-formed XML; company names can also contain bare ampersands.
    Escape these for indexing, only after strict
    parsing failed and only in DOCUMENT files declaring a DART schema. Keep a
    repair ledger and retry strict parsing; never use a lossy HTML tree.
    """
    try:
        return xml_root(raw), {"mode": "strict_xml", "repairs": []}
    except DartError as exc:
        if exc.code != "invalid_xml":
            raise
    if not re.search(r'<DOCUMENT\b[^>]*\bnoNamespaceSchemaLocation=[\"\']dart[0-9.]+\.xsd[\"\']', decoded):
        raise DartError("invalid_xml")
    repaired = []

    def escape(match):
        label = match.group(1)
        # A paired Korean element could be intentional. Do not reinterpret it.
        if re.search(r"</" + re.escape(label.strip()) + r"\s*>", decoded):
            return match.group(0)
        repaired.append({"kind": "literal_angle_label", "literal": match.group(0)})
        return "&lt;" + label + "&gt;"

    def amp(match):
        repaired.append({"kind": "bare_ampersand"})
        return "&amp;"

    parts = _PROTECTED.split(decoded)
    for index in range(0, len(parts), 2):
        parts[index] = _BARE_AMP.sub(amp, _LABEL.sub(escape, parts[index]))
    if not repaired:
        raise DartError("invalid_xml")
    # Unicode input must not retain a conflicting original encoding declaration.
    text = "".join(parts)
    text = re.sub(r"^\s*<\?xml[^?]*\?>", "", text, count=1)
    root = xml_root(text.encode("utf-8"))
    return root, {"mode": "dart_literal_text", "repairs": repaired}
