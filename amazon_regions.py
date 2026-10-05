"""Amazon retail marketplaces; delivery countries are a separate concept."""
MARKETPLACES=[
 ('us','United States','🇺🇸','USD','amazon.com'),('jp','Japan','🇯🇵','JPY','amazon.co.jp'),
 ('gb','United Kingdom','🇬🇧','GBP','amazon.co.uk'),('de','Germany','🇩🇪','EUR','amazon.de'),
 ('ca','Canada','🇨🇦','CAD','amazon.ca'),('mx','Mexico','🇲🇽','MXN','amazon.com.mx'),
 ('br','Brazil','🇧🇷','BRL','amazon.com.br'),('fr','France','🇫🇷','EUR','amazon.fr'),
 ('it','Italy','🇮🇹','EUR','amazon.it'),('es','Spain','🇪🇸','EUR','amazon.es'),
 ('nl','Netherlands','🇳🇱','EUR','amazon.nl'),('be','Belgium','🇧🇪','EUR','amazon.com.be'),
 ('ie','Ireland','🇮🇪','EUR','amazon.ie'),('se','Sweden','🇸🇪','SEK','amazon.se'),
 ('pl','Poland','🇵🇱','PLN','amazon.pl'),('tr','Turkey','🇹🇷','TRY','amazon.com.tr'),
 ('ae','United Arab Emirates','🇦🇪','AED','amazon.ae'),('sa','Saudi Arabia','🇸🇦','SAR','amazon.sa'),
 ('eg','Egypt','🇪🇬','EGP','amazon.eg'),('za','South Africa','🇿🇦','ZAR','amazon.co.za'),
 ('in','India','🇮🇳','INR','amazon.in'),('sg','Singapore','🇸🇬','SGD','amazon.sg'),
 ('au','Australia','🇦🇺','AUD','amazon.com.au')]
# Keep the current 23 storefronts explicit; do not invent storefronts for delivery destinations.
REGIONS=[r[:4] for r in MARKETPLACES]
AMAZON_DOMAINS={r[0]:r[4] for r in MARKETPLACES}

def marketplace(host):
    host=(host or '').lower()
    return next((r for r in MARKETPLACES if host==r[4] or host.endswith('.'+r[4])),None)
