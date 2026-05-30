"""Constants for the 西安水务 integration."""

DOMAIN = "xian_water"
NAME = "西安水费"

CONF_CLIENT_CODE = "client_code"
CONF_CLIENT_TYPE = "client_type"
CONF_CID = "cid"

DEFAULT_CLIENT_CODE = "002024195152"
DEFAULT_CLIENT_TYPE = "IC"
DEFAULT_CID = "00FE4A8A2E"
DEFAULT_PAGE_SIZE = 10

API_ENDPOINT = "http://dzfp.xazls.com:54432/invoice/ew/queryPayRecords"

ATTR_PRICE = "price"
ATTR_BALANCE = "balance"
ATTR_USAGE_DAYS = "usage_days"
ATTR_DATA = "data"