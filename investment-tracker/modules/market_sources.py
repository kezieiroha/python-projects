"""Market-source configuration helpers."""

DEFAULT_MACRO_TICKERS = {
    "TBC",
    "XRP-USD",
    "ETH-USD",
    "BTC",
    "CRCL",
    "VIX",
    "TNX",
    "CL=F",
    "DXY",
    "TLT",
}

DEFAULT_SENTIMENT_TICKERS = {"CL=F", "VIX", "TNX", "TLT", "DXY"}


def ticker_set(config, key, default):
    values = config.get(key, default)
    if not isinstance(values, list):
        values = default
    return {str(v).upper() for v in values}


def macro_tickers(config):
    return ticker_set(config, "macro_tickers", DEFAULT_MACRO_TICKERS)


def sentiment_tickers(config):
    return ticker_set(config, "sentiment_tickers", DEFAULT_SENTIMENT_TICKERS)

