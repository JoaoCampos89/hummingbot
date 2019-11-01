class VeridexConstants:
    """
    Constants class stores all of the constants required for VERIDEX connector module
    """
    # Rest API endpoints
    REST_BASE_URL = 'https://veridex.herokuapp.com/v2/0x'
    WS_URL = 'wss://veridex.herokuapp.com'
    GET_TOKENS_URL = REST_BASE_URL + '/tokens'
    GET_MARKETS_URL = REST_BASE_URL + '/markets'
