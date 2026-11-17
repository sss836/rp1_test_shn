"""Validate query replies without confusing the drive-state bit with an error."""


def valid_query_header(value):
    # Native canfd_rx_cbk and the production parameter script classify queries
    # by bits 7..5 (0xA0). Bit 4 is present in the field's 109 B0 responses.
    # Continue rejecting nonzero low error/reserved bits, rather than treating
    # every frame in that class as a successful response.
    return (value & 0xE0) == 0xA0 and (value & 0x0F) == 0
