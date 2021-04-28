from openwpm import CommandSequence, TaskManager
import csv

NUM_BROWSERS = 1
sites = []
with open("dataset.csv") as csvfile:
    reader = csv.reader(csvfile, quoting=csv.QUOTE_NONE)
    for row in reader:
        element = row[1]
        if "www" in element:
            element = "http://" + element
        else:
            element = "http://www." + element
        sites.append(element)

manager_params, browser_params = TaskManager.load_default_params(NUM_BROWSERS)

# Update browser configuration (use this for per-browser settings)
for i in range(NUM_BROWSERS):
    # Record HTTP Requests and Responses
    browser_params[i]["http_instrument"] = False
    # Record cookie changes
    browser_params[i]["cookie_instrument"] = True
    # Record Navigations
    browser_params[i]["navigation_instrument"] = False
    # Record JS Web API calls
    browser_params[i]["js_instrument"] = False
    # Record the callstack of all WebRequests made
    browser_params[i]["callstack_instrument"] = False
    # Record DNS resolution
    browser_params[i]["dns_instrument"] = False
    # browser_params[i]["save_content"] = "script"

# Launch only browser 0 headless
# browser_params[0]["display_mode"] = "native"

# Update TaskManager configuration (use this for crawl-wide settings)
manager_params["data_directory"] = "~/Desktop/output/test/"
manager_params["log_directory"] = "~/Desktop/output/test/"
manager_params["memory_watchdog"] = True
manager_params["process_watchdog"] = True

# Instantiates the measurement platform
# Commands time out by default after 60 seconds
manager = TaskManager.TaskManager(manager_params, browser_params)

# Visits the sites
for site in sites:
    # Parallelize sites over all number of browsers set above.
    command_sequence = CommandSequence.CommandSequence(
        site,
        reset=True,
        callback=lambda success, val=site: print("CommandSequence {} done".format(val)),
    )

    # Start by visiting the page
    command_sequence.get(sleep=3, timeout=300)
    command_sequence.detect_dark_patterns(sleep=3, timeout=300)
    # command_sequence.ping_cmp(sleep=3, timeout=300)
    # command_sequence.detect_cookie_dialog(sleep=3, timeout=300)

    # Run commands across the three browsers (simple parallelization)
    manager.execute_command_sequence(command_sequence)

# Shuts down the browsers and waits for the data to finish logging
manager.close()
