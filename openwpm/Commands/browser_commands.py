import gzip
import json
import logging
import os
import random
import sys
import time
import traceback
from glob import glob
from hashlib import md5
import sqlite3
import re

from PIL import Image
from selenium.common.exceptions import (
    MoveTargetOutOfBoundsException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.firefox.options import Options

from ..SocketInterface import clientsocket
from .utils.webdriver_utils import (
    execute_in_all_frames,
    execute_script_with_retry,
    get_intra_links,
    is_displayed,
    scroll_down,
    wait_until_loaded,
)
from google_trans_new import google_translator

# Constants for bot mitigation
NUM_MOUSE_MOVES = 10  # Times to randomly move the mouse
RANDOM_SLEEP_LOW = 1  # low (in sec) for random sleep between page loads
RANDOM_SLEEP_HIGH = 7  # high (in sec) for random sleep between page loads
logger = logging.getLogger("openwpm")


def bot_mitigation(webdriver):
    """performs three optional commands for bot-detection
    mitigation when getting a site"""

    # bot mitigation 1: move the randomly around a number of times
    window_size = webdriver.get_window_size()
    num_moves = 0
    num_fails = 0
    while num_moves < NUM_MOUSE_MOVES + 1 and num_fails < NUM_MOUSE_MOVES:
        try:
            if num_moves == 0:  # move to the center of the screen
                x = int(round(window_size["height"] / 2))
                y = int(round(window_size["width"] / 2))
            else:  # move a random amount in some direction
                move_max = random.randint(0, 500)
                x = random.randint(-move_max, move_max)
                y = random.randint(-move_max, move_max)
            action = ActionChains(webdriver)
            action.move_by_offset(x, y)
            action.perform()
            num_moves += 1
        except MoveTargetOutOfBoundsException:
            num_fails += 1
            pass

    # bot mitigation 2: scroll in random intervals down page
    scroll_down(webdriver)

    # bot mitigation 3: randomly wait so page visits happen with irregularity
    time.sleep(random.randrange(RANDOM_SLEEP_LOW, RANDOM_SLEEP_HIGH))


def close_other_windows(webdriver):
    """
    close all open pop-up windows and tabs other than the current one
    """
    main_handle = webdriver.current_window_handle
    windows = webdriver.window_handles
    if len(windows) > 1:
        for window in windows:
            if window != main_handle:
                webdriver.switch_to_window(window)
                webdriver.close()
        webdriver.switch_to_window(main_handle)


def tab_restart_browser(webdriver):
    """
    kills the current tab and creates a new one to stop traffic
    """
    # note: this technically uses windows, not tabs, due to problems with
    # chrome-targeted keyboard commands in Selenium 3 (intermittent
    # nonsense WebDriverExceptions are thrown). windows can be reliably
    # created, although we do have to detour into JS to do it.
    close_other_windows(webdriver)

    if webdriver.current_url.lower() == "about:blank":
        return

    # Create a new window.  Note that it is not practical to use
    # noopener here, as we would then be forced to specify a bunch of
    # other "features" that we don't know whether they are on or off.
    # Closing the old window will kill the opener anyway.
    webdriver.execute_script("window.open('')")

    # This closes the _old_ window, and does _not_ switch to the new one.
    webdriver.close()

    # The only remaining window handle will be for the new window;
    # switch to it.
    assert len(webdriver.window_handles) == 1
    webdriver.switch_to_window(webdriver.window_handles[0])


def get_website(
        url, sleep, visit_id, webdriver, browser_params, extension_socket: clientsocket
):
    """
    goes to <url> using the given <webdriver> instance
    """

    tab_restart_browser(webdriver)

    if extension_socket is not None:
        extension_socket.send(visit_id)

    # Execute a get through selenium
    try:
        webdriver.get(url)
    except TimeoutException:
        pass

    # Sleep after get returns
    time.sleep(sleep)

    # Close modal dialog if exists
    try:
        WebDriverWait(webdriver, 0.5).until(EC.alert_is_present())
        alert = webdriver.switch_to_alert()
        alert.dismiss()
        time.sleep(1)
    except (TimeoutException, WebDriverException):
        pass

    close_other_windows(webdriver)

    if browser_params["bot_mitigation"]:
        bot_mitigation(webdriver)


def browse_website(
        url,
        num_links,
        sleep,
        visit_id,
        webdriver,
        browser_params,
        manager_params,
        extension_socket,
):
    """Calls get_website before visiting <num_links> present on the page.

    Note: the site_url in the site_visits table for the links visited will
    be the site_url of the original page and NOT the url of the links visited.
    """
    # First get the site
    get_website(url, sleep, visit_id, webdriver, browser_params, extension_socket)

    # Then visit a few subpages
    for _ in range(num_links):
        links = [x for x in get_intra_links(webdriver, url) if is_displayed(x) is True]
        if not links:
            break
        r = int(random.random() * len(links))
        logger.info(
            "BROWSER %i: visiting internal link %s"
            % (browser_params["browser_id"], links[r].get_attribute("href"))
        )

        try:
            links[r].click()
            wait_until_loaded(webdriver, 300)
            time.sleep(max(1, sleep))
            if browser_params["bot_mitigation"]:
                bot_mitigation(webdriver)
            webdriver.back()
            wait_until_loaded(webdriver, 300)
        except Exception:
            pass


def convert_rgb_to_hex(rgb):
    if rgb is None or rgb == "rgba(0, 0, 0, 0)":
        return
    result = re.search(r'rgb\((\d+),\s*(\d+),\s*(\d+)', rgb)
    if result is not None:
        r, g, b = map(int, result.groups())
        return '#%02x%02x%02x' % (r, g, b)

    result = re.search(r'rgba\((\d+),\s*(\d+),\s*(\d+),\s*(\d+)', rgb)
    if result is not None:
        r, g, b, a = map(int, result.groups())
        return '#%02x%02x%02x%02x' % (r, g, b, a)

    return


def save_screenshot(visit_id, browser_id, driver, manager_params, suffix=""):
    """ Save a screenshot of the current viewport"""
    if suffix != "":
        suffix = "-" + suffix

    urlhash = md5(driver.current_url.encode("utf-8")).hexdigest()
    outname = os.path.join(
        manager_params["screenshot_path"], "%i-%s%s.png" % (visit_id, urlhash, suffix)
    )
    driver.save_screenshot(outname)


def check_html_elements(webdriver, buttons, reject=False):
    # translator = google_translator()
    elements = None

    for b in buttons:
        try:
            # 429 (Too Many Requests) from TTS API. Probable cause: Unknown
            # https://github.com/lushan88a/google_trans_new/issues/28
            # translation = translator.translate(b, lang_src="nl", lang_tgt=language)
            if reject:
                elements = webdriver.find_elements_by_xpath(
                    "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), {value}) or "
                    "contains(translate(@aria-label, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), {value})]|"
                    "//a[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'), {value})]|"
                    "//span[contains(@class,'a-button-inner') and "
                    "contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), {value})]|"
                    "//input[contains(translate(@value,'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), {value})]"
                        .format(
                        value='\"' + b + '\"'))
                if elements is None or len(elements) == 0:
                    elements = webdriver.find_elements_by_xpath("//div[string-length(.) < 20 and contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), {value})]"
                        .format(value='\"' + b + '\"'))
            else:
                elements = webdriver.find_elements_by_xpath(
                    "//button[(contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), {value}) or "
                    "contains(translate(@aria-label, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), {value})) and "
                    "not("
                    "contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'niet') or "
                    "contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'not') or "
                    "contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '...')"
                    ")]|"
                    "//button[normalize-space(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'))='ok']|"
                    "//a[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'), {value}) and "
                    "not("
                    "contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'niet') or "
                    "contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'not') or "
                    "contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '...')"
                    ")]|"
                    "//a[normalize-space(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'))='ok']|"
                    "//span[contains(@class,'a-button-inner') and "
                    "contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), {value})]|"
                    "//input[contains(translate(@value,'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), {value})]"
                        .format(
                        value='\"' + b + '\"'))
                if elements is None or len(elements) == 0:
                    elements = webdriver.find_elements_by_xpath("//div[string-length(.) < 20 and contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), {value})]"
                        .format(value='\"' + b + '\"'))
            if elements is not None and len(elements) > 0:
                for e in elements:
                    if len(e.text) < 50 and e.size["width"] != 0 and e.size["height"] != 0:
                        return {
                            "text": e.text,
                            "width": e.size["width"],
                            "height": e.size["height"],
                            "bgColor": e.value_of_css_property("background-color")
                        }
                    else:
                        continue
                continue
            else:
                continue
        except Exception as err:
            print(err)
            continue

    if not reject and (elements is None or len(elements) == 0):
        elements = webdriver.find_elements_by_xpath(
            "//button[normalize-space(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'))='ok' and "
            "not("
            "contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'niet') or "
            "contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'not') or "
            "contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '...')"
            ")]|"
            "//a[normalize-space(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'))='ok' and "
            "not("
            "contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'niet') or "
            "contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'not') or "
            "contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '...')"
            ")]")

        if elements is not None and len(elements) > 0:
            for e in elements:
                if len(e.text) < 50 and e.size["width"] != 0 and e.size["height"] != 0:
                    return {
                        "text": e.text,
                        "width": e.size["width"],
                        "height": e.size["height"],
                        "bgColor": e.value_of_css_property("background-color")
                    }
                else:
                    continue

    return None


def check_iframes(webdriver, buttons, reject=False):
    iframe = None
    # translator = google_translator()
    elements = None

    try:
        frames = webdriver.find_elements_by_tag_name("iframe")
        for frame in frames:
            matches = ["cmp", "consent"]
            if any(x in frame.get_attribute("src") for x in matches):
                iframe = frame
                break
        if len(frames) > 0 and iframe is None:
            iframe = frames[0]
        if iframe is not None:
            webdriver.switch_to.frame(iframe)
            for b in buttons:
                try:
                    # lang = translator.detect(b)
                    #if lang is not None and len(lang) > 0 and lang[0] != "nl" and lang[0] != "af":
                    # translation = translator.translate(b, lang_src="nl", lang_tgt=language)
                    if reject:
                        elements = webdriver.find_elements_by_xpath(
                            "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), {value}) or "
                            "contains(translate(@aria-label, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), {value})]|"
                            "//a[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'), {value})]|"
                            "//span[contains(@class,'a-button-inner') and "
                            "contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), {value})]|"
                            "//input[contains(translate(@value,'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), {value})]"
                                .format(
                                value='\"' + b + '\"'))
                        if elements is None or len(elements) == 0:
                            elements = webdriver.find_elements_by_xpath(
                                "//div[string-length(.) < 20 and contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), {value})]"
                                .format(
                                    value='\"' + b + '\"'))
                    else:
                        elements = webdriver.find_elements_by_xpath(
                            "//button[(contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), {value}) or "
                            "contains(translate(@aria-label, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), {value})) and "
                            "not("
                            "contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'niet') or "
                            "contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'not') or "
                            "contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '...')"
                            ")]|"
                            "//button[normalize-space(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'))='ok']|"
                            "//a[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'), {value}) and "
                            "not("
                            "contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'niet') or "
                            "contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'not') or "
                            "contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '...')"
                            ")]|"
                            "//a[normalize-space(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'))='ok']|"
                            "//span[contains(@class,'a-button-inner') and "
                            "contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), {value})]|"
                            "//input[contains(translate(@value,'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), {value})]"
                                .format(
                                value='\"' + b + '\"'))
                        if elements is None or len(elements) == 0:
                            elements = webdriver.find_elements_by_xpath(
                                "//div[string-length(.) < 20 and contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), {value})]"
                                .format(
                                    value='\"' + b + '\"'))
                    if elements is not None and len(elements) > 0:
                        for e in elements:
                            if len(e.text) < 50 and e.size["width"] != 0 and e.size["height"] != 0:
                                return {
                                    "text": e.text,
                                    "width": e.size["width"],
                                    "height": e.size["height"],
                                    "bgColor": e.value_of_css_property("background-color")
                                }
                            else:
                                continue
                        continue
                    else:
                        continue
                except Exception:
                    continue

            if not reject and (elements is None or len(elements) == 0):
                elements = webdriver.find_elements_by_xpath(
                    "//button[normalize-space(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'))='ok' and "
                    "not("
                    "contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'niet') or "
                    "contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'not') or "
                    "contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '...')"
                    ")]|"
                    "//a[normalize-space(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'))='ok' and "
                    "not("
                    "contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'niet') or "
                    "contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'not') or "
                    "contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '...')"
                    ")]")

                if elements is not None and len(elements) > 0:
                    for e in elements:
                        if len(e.text) < 50 and e.size["width"] != 0 and e.size["height"] != 0:
                            return {
                                "text": e.text,
                                "width": e.size["width"],
                                "height": e.size["height"],
                                "bgColor": e.value_of_css_property("background-color")
                            }
                        else:
                            continue
    except Exception:
        pass

    return None


def detect_dark_patterns(visit_id, webdriver, languages):
    allow_button_exists = 0
    reject_button_exists = 0
    consent_element = None
    reject_element = None
    element_found = False

    print(languages)

    for lang in languages:
        with open("dark_patterns_detection/consent_{}.txt".format(lang)) as f:
            allow_buttons = f.read().splitlines()

        with open("dark_patterns_detection/reject_{}.txt".format(lang)) as f:
            reject_buttons = f.read().splitlines()

        consent_element = check_html_elements(webdriver, allow_buttons)
        if consent_element is None:
            consent_element = check_iframes(webdriver, allow_buttons)
            webdriver.switch_to.default_content()

        reject_element = check_html_elements(webdriver, reject_buttons, True)
        if reject_element is None:
            reject_element = check_iframes(webdriver, reject_buttons, True)
            webdriver.switch_to.default_content()

        if consent_element is None and reject_element is None:
            continue

        element_found = True

        if consent_element is not None:
            allow_button_exists = 1

        if reject_element is not None:
            reject_button_exists = 1

        break

    if not element_found:
        return

    openwpm_db = "/home/parallels/Desktop/output/crawl-data.sqlite"
    conn = sqlite3.connect(openwpm_db, timeout=300)
    cur = conn.cursor()
    cur.execute("pragma journal_mode=wal3")
    cur.execute("INSERT INTO dark_patterns VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (visit_id,
                 allow_button_exists,
                 consent_element.get("text") if consent_element is not None else "",
                 round(consent_element.get("width")) if consent_element is not None else 0,
                 round(consent_element.get("height")) if consent_element is not None else 0,
                 consent_element.get("bgColor") if consent_element is not None else 0,
                 convert_rgb_to_hex(consent_element.get("bgColor")) if consent_element is not None else 0,
                 reject_button_exists,
                 reject_element.get("text") if reject_element is not None else "",
                 round(reject_element.get("width")) if reject_element is not None else 0,
                 round(reject_element.get("height")) if reject_element is not None else 0,
                 reject_element.get("bgColor") if reject_element is not None else 0,
                 convert_rgb_to_hex(reject_element.get("bgColor")) if reject_element is not None else 0,
                 ))
    conn.commit()
    conn.close()


def ping_cmp(visit_id, webdriver):
    tc_data = webdriver.execute_script(
        "let result = null; "
        "if (typeof __tcfapi == 'function') { "
        "   window.__tcfapi('ping', 2, function(tcData, success) { "
        "       result = Object.assign({}, tcData); "
        "   }); "
        "} "
        "return result;")

    if tc_data is not None:
        cmp_name = ""
        if "cmpId" in tc_data.keys():
            with open("cmplist.json") as json_file:
                data = json.load(json_file)
                for key, value in data["cmps"].items():
                    if int(key) == tc_data["cmpId"]:
                        cmp_name = value["name"]
                        break
        openwpm_db = "/home/parallels/Desktop/output/crawl-data.sqlite"
        # openwpm_db = "/opt/Desktop/output/crawl-data.sqlite"
        conn = sqlite3.connect(openwpm_db, timeout=300)
        cur = conn.cursor()
        cur.execute("pragma journal_mode=wal3")
        cur.execute("INSERT INTO ping_cmp VALUES (?,?,?,?,?)",
                    (visit_id, tc_data["cmpId"], cmp_name, tc_data["tcfPolicyVersion"], tc_data["gdprApplies"]))
        conn.commit()
        conn.close()


def detect_cookie_dialog(visit_id, webdriver):
    element = None
    element_type = ""

    try:
        frames = webdriver.find_elements_by_tag_name("iframe")
        for frame in frames:
            webdriver.switch_to.default_content()
            matches = ["cmp", "consent", "cookie"]
            if any(x in frame.get_attribute("src") for x in matches):
                element = frame
                element_type = "frame"
                break
            else:
                try:
                    webdriver.switch_to.frame(frame)
                    element = webdriver.find_element_by_xpath(
                        "//*[contains(@class, {}) or contains(@class, {}) or contains(@class, {})]".format(
                            "'" + "banner" + "'",
                            "'" + "consent" + "'",
                            "'" + "cmp" + "'"))
                    element_type = "frame"
                    break
                except Exception:
                    continue
    except Exception:
        pass

    webdriver.switch_to.default_content()

    if element is None:
        with open("cookie_dialog_ids.txt") as f:
            ids = f.read().splitlines()

        for i in ids:
            try:
                # xpath_id = "//*[@id={}]".format('"' + class_id + '"')
                element = webdriver.find_element_by_xpath(
                    "//*[translate(@id, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')={}]".format(
                        "'" + i + "'"))
                element_type = "id"
                break
            except Exception:
                continue

        if element is None:
            with open("cookie_dialog_classes.txt") as f:
                classes = f.read().splitlines()
            for c in classes:
                try:
                    element = webdriver.find_element_by_xpath(
                        "//*[translate(@class, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')={}]".format(
                            "'" + c + "'"))
                    element_type = "class"
                    break
                except Exception:
                    continue

    found = 0
    if element is not None:
        found = 1

    openwpm_db = "/home/parallels/Desktop/output/crawl-data.sqlite"
    # openwpm_db = "/opt/Desktop/output/crawl-data.sqlite"
    conn = sqlite3.connect(openwpm_db, timeout=300)
    cur = conn.cursor()
    cur.execute("pragma journal_mode=wal3")
    cur.execute("INSERT INTO cookie_dialog VALUES (?,?,?)",
                (visit_id, found, element_type))
    conn.commit()
    conn.close()


def disable_javascript(visit_id, webdriver):
    options = Options()
    options.preferences.update({'javascript.enabled': False})


def _stitch_screenshot_parts(visit_id, browser_id, manager_params):
    # Read image parts and compute dimensions of output image
    total_height = -1
    max_scroll = -1
    max_width = -1
    images = dict()
    parts = list()
    for f in glob(
            os.path.join(
                manager_params["screenshot_path"], "parts", "%i*-part-*.png" % visit_id
            )
    ):

        # Load image from disk and parse params out of filename
        img_obj = Image.open(f)
        width, height = img_obj.size
        parts.append((f, width, height))
        outname, _, index, curr_scroll = os.path.basename(f).rsplit("-", 3)
        curr_scroll = int(curr_scroll.split(".")[0])
        index = int(index)

        # Update output image size
        if curr_scroll > max_scroll:
            max_scroll = curr_scroll
            total_height = max_scroll + height

        if width > max_width:
            max_width = width

        # Save image parameters
        img = {}
        img["object"] = img_obj
        img["scroll"] = curr_scroll
        images[index] = img

    # Output filename same for all parts, so we can just use last filename
    outname = outname + ".png"
    outname = os.path.join(manager_params["screenshot_path"], outname)
    output = Image.new("RGB", (max_width, total_height))

    # Compute dimensions for output image
    for i in range(max(images.keys()) + 1):
        img = images[i]
        output.paste(im=img["object"], box=(0, img["scroll"]))
        img["object"].close()
    try:
        output.save(outname)
    except SystemError:
        logger.error(
            "BROWSER %i: SystemError while trying to save screenshot %s. \n"
            "Slices of image %s \n Final size %s, %s."
            % (
                browser_id,
                outname,
                "\n".join([str(x) for x in parts]),
                max_width,
                total_height,
            )
        )
        pass


def screenshot_full_page(visit_id, browser_id, driver, manager_params, suffix=""):
    outdir = os.path.join(manager_params["screenshot_path"], "parts")
    if not os.path.isdir(outdir):
        os.mkdir(outdir)
    if suffix != "":
        suffix = "-" + suffix
    urlhash = md5(driver.current_url.encode("utf-8")).hexdigest()
    outname = os.path.join(
        outdir, "%i-%s%s-part-%%i-%%i.png" % (visit_id, urlhash, suffix)
    )

    try:
        part = 0
        max_height = execute_script_with_retry(
            driver, "return document.body.scrollHeight;"
        )
        inner_height = execute_script_with_retry(driver, "return window.innerHeight;")
        curr_scrollY = execute_script_with_retry(driver, "return window.scrollY;")
        prev_scrollY = -1
        driver.save_screenshot(outname % (part, curr_scrollY))
        while (
                curr_scrollY + inner_height
        ) < max_height and curr_scrollY != prev_scrollY:

            # Scroll down to bottom of previous viewport
            try:
                driver.execute_script("window.scrollBy(0, window.innerHeight)")
            except WebDriverException:
                logger.info(
                    "BROWSER %i: WebDriverException while scrolling, "
                    "screenshot may be misaligned!" % browser_id
                )
                pass

            # Update control variables
            part += 1
            prev_scrollY = curr_scrollY
            curr_scrollY = execute_script_with_retry(driver, "return window.scrollY;")

            # Save screenshot
            driver.save_screenshot(outname % (part, curr_scrollY))
    except WebDriverException:
        excp = traceback.format_exception(*sys.exc_info())
        logger.error(
            "BROWSER %i: Exception while taking full page screenshot \n %s"
            % (browser_id, "".join(excp))
        )
        return

    _stitch_screenshot_parts(visit_id, browser_id, manager_params)


def dump_page_source(visit_id, driver, manager_params, suffix=""):
    if suffix != "":
        suffix = "-" + suffix

    outname = md5(driver.current_url.encode("utf-8")).hexdigest()
    outfile = os.path.join(
        manager_params["source_dump_path"], "%i-%s%s.html" % (visit_id, outname, suffix)
    )

    with open(outfile, "wb") as f:
        f.write(driver.page_source.encode("utf8"))
        f.write(b"\n")


def recursive_dump_page_source(visit_id, driver, manager_params, suffix=""):
    """Dump a compressed html tree for the current page visit"""
    if suffix != "":
        suffix = "-" + suffix

    outname = md5(driver.current_url.encode("utf-8")).hexdigest()
    outfile = os.path.join(
        manager_params["source_dump_path"],
        "%i-%s%s.json.gz" % (visit_id, outname, suffix),
    )

    def collect_source(driver, frame_stack, rv={}):
        is_top_frame = len(frame_stack) == 1

        # Gather frame information
        doc_url = driver.execute_script("return window.document.URL;")
        if is_top_frame:
            page_source = rv
        else:
            page_source = dict()
        page_source["doc_url"] = doc_url
        source = driver.page_source
        if type(source) != str:
            source = str(source, "utf-8")
        page_source["source"] = source
        page_source["iframes"] = dict()

        # Store frame info in correct area of return value
        if is_top_frame:
            return
        out_dict = rv["iframes"]
        for frame in frame_stack[1:-1]:
            out_dict = out_dict[frame.id]["iframes"]
        out_dict[frame_stack[-1].id] = page_source

    page_source = dict()
    execute_in_all_frames(driver, collect_source, {"rv": page_source})

    with gzip.GzipFile(outfile, "wb") as f:
        f.write(json.dumps(page_source).encode("utf-8"))


def finalize(
        visit_id: int, webdriver: WebDriver, extension_socket: clientsocket, sleep: int
) -> None:
    """ Informs the extension that a visit is done """
    tab_restart_browser(webdriver)
    # This doesn't immediately stop data saving from the current
    # visit so we sleep briefly before unsetting the visit_id.
    time.sleep(sleep)
    msg = {"action": "Finalize", "visit_id": visit_id}
    extension_socket.send(msg)


def initialize(visit_id: int, extension_socket: clientsocket) -> None:
    msg = {"action": "Initialize", "visit_id": visit_id}
    extension_socket.send(msg)
