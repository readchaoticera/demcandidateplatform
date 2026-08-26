"""Source adapters.

Each source knows something the others do not: the FEC has the filing universe
and canonical IDs but not primary results; Wikipedia has results; Ballotpedia
has campaign URLs. ``dcp.resolve`` merges them.
"""
