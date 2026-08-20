"""Show a contact name instead of a raw phone number or email address.

The name goes into the chunk TEXT only. The stored identifier list keeps the
raw phone or email, because that is the join key for the next contacts
update and a display name cannot be re-mapped. Putting the name in the text
also fixes full-text search for free, since tsv is a generated column.
"""


def label(identifier, contacts):
    """The contact name, or the identifier unchanged.

    Unknown identifiers stay as they are rather than becoming "unknown":
    most participants have no contact, and one shared bucket would merge
    separate people into a single apparent speaker.
    """
    if not identifier:
        return ""
    if identifier in contacts:
        return contacts[identifier]
    return contacts.get(identifier.strip().lower(), identifier)


def header(identifiers, contacts):
    """Sorted, de-duplicated labels for a chunk header.

    Sorting keeps the text stable, so an unchanged chunk does not look
    changed and re-embed for nothing.
    """
    return ", ".join(sorted({label(i, contacts) for i in identifiers if i})) \
        or "unknown"
