import os

# Tests choose their own settings; never pick any up from a developer's .env
# (the package's .env loader only fills variables that are not already set).
os.environ.update({
    "CRM_BACKEND": "sqlite",
    "HUBSPOT_SERVICE_KEY": "",
    "HUBSPOT_OWNER_PROPERTY": "mcp_sales_rep",
    "HUBSPOT_LAST_ACTIVITY_PROPERTY": "mcp_last_activity_date",
    "HUBSPOT_NEXT_STEP_DATE_PROPERTY": "mcp_next_step_date",
})
