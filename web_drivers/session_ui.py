"""Delete only a conversation whose exact website ID belongs to this task."""
import re
import time
from urllib.parse import parse_qs, urlsplit


def detect_session_id(name, url):
    parsed = urlsplit(url or "")
    from web_drivers.account_status import _SITES
    if parsed.scheme != "https" or parsed.hostname not in _SITES.get(name, ()):
        return None
    if name == "ChatGLM":
        cid = (parse_qs(parsed.query).get("cid") or [""])[0]
        return cid if re.fullmatch(r"[a-fA-F0-9]{24}", cid) else None
    pattern = (r"/chat/[^/]+/([a-zA-Z0-9_-]{6,})/?$" if name == "Yuanbao"
               else r"/chat/([a-zA-Z0-9_-]{6,})/?$")
    match = re.search(pattern, parsed.path)
    sid = match.group(1) if match else None
    if name == "Kimi" and sid and re.fullmatch(r"p[0-9a-f]{7}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", sid):
        return None  # client-side provisional ID, not the persisted history ID
    return sid


def _visible(page, selector):
    return page.locator(selector).filter(visible=True)


def _owned_row(page, name, sid):
    if name == "ChatGLM" and detect_session_id(name, page.url) == sid:
        selected = _visible(page, '.history-item.selected')
        header = _visible(page, '.conversation-name')
        if selected.count() == 1 and header.count() and (
                selected.locator('.title').inner_text().strip() == header.first.inner_text().strip()):
            selected.evaluate('(e,sid)=>e.setAttribute("data-aq-owned-session",sid)', sid)
        return page.locator('[data-aq-owned-session="' + sid + '"]')
    if name == "Yuanbao":
        return page.locator('.yb-recent-conv-list__item').filter(
            has=page.locator(f'[data-item-id="{sid}"]'))
    if name == "Kimi":
        return page.locator('.next-sidebar-history-item').filter(
            has=page.locator(f'a[href^="/chat/{sid}"]'))
    if name == "Qianwen":
        return page.locator(f'div[data-session-id="{sid}"][data-session-switch-target="normal"]')
    # Other adapters register a DOM marker only after finding an exact ID in
    # their own sidebar. A title match never authorizes conversation deletion.
    return page.locator('[data-aq-owned-session="' + sid + '"]')


def delete_owned_session(driver):
    name, sid = driver.provider_name, driver._session_id
    if not sid or not re.fullmatch(r"[a-zA-Z0-9_-]{6,}", sid):
        return False
    page = driver._page_instance()
    if name == "ChatGLM" and detect_session_id(name, page.url) == sid:
        until = time.monotonic() + 5
        while not _owned_row(page, name, sid).count() and time.monotonic() < until:
            page.wait_for_timeout(250)
    if name == "ChatGLM" and detect_session_id(name, page.url) != sid:
        # Use the site's sidebar SPA navigation; direct conversation URLs can
        # trigger a separate verification interstitial. Selection by title
        # alone never authorizes deletion: verify the resulting exact URL ID.
        rows = page.locator('.history-item')
        for index in range(rows.count()):
            rows.nth(index).locator('.title').click(timeout=4000)
            page.wait_for_timeout(350)
            if detect_session_id(name, page.url) == sid:
                until = time.monotonic() + 5
                while not _owned_row(page, name, sid).count() and time.monotonic() < until:
                    page.wait_for_timeout(250)
                break
        else:
            return False
    if name == "Kimi":
        expand = _visible(page, '[role="button"][aria-label="展开导航"]')
        if expand.count() == 1:
            expand.click(timeout=4000)
            page.wait_for_timeout(300)
    row = _owned_row(page, name, sid)
    if row.count() != 1:
        return False
    row.scroll_into_view_if_needed(timeout=4000)
    row.hover(timeout=4000)
    more = row.locator('[aria-label="更多"],.yb-recent-conv-list__dropdown-trigger,.next-sidebar-history-item__more,.option,[data-session-switch-target="session-tools"]')
    if more.count() != 1:
        return False
    more.click(timeout=4000)
    if name == "ChatGLM":
        page.wait_for_timeout(300)
        clicked = driver._safe_evaluate("""sid=>{
          const row=document.querySelector('[data-aq-owned-session="'+sid+'"]');
          const item=row&&[...row.querySelectorAll('.operate-item')].find(e=>
            e.innerText.trim()==='删除对话'&&(e.offsetWidth||e.offsetHeight));
          if(!item)return false;item.click();return true;
        }""", sid)
        if not clicked:
            return False
    else:
        delete = _visible(page, '[role="menuitem"], [role="menu"] button,.operate-item,.yb-dropdown__item-text').filter(
            has_text=re.compile(r"^\s*删除(?:此对话|对话)?\s*$"))
        if delete.count() != 1:
            return False
        delete.click(timeout=4000)
    page.wait_for_timeout(300)
    dialog = _visible(page, '[role="dialog"]')
    if dialog.count() == 1:
        confirm = dialog.get_by_role("button", name=re.compile(r"^(删除|确认删除|确定)$"))
        if not confirm.count() and name == "ChatGLM":
            confirm = dialog.locator('.btn.sure')
        if confirm.count() != 1:
            return False
        confirm.click(timeout=4000)
    elif name == "ChatGLM":
        confirm = _visible(page, '.dialog-container .btn.sure,.el-dialog .btn.sure,.common_dialog .btn.sure')
        if confirm.count() != 1:
            return False
        confirm.click(timeout=4000)
    elif name == "Yuanbao":
        confirm = _visible(page, 'button').filter(has_text=re.compile(r"^\s*确认删除\s*$"))
        if confirm.count() != 1:
            return False
        confirm.click(timeout=4000)
    elif name == "Kimi":
        confirm = _visible(page, '[data-testid="confirm-dialog-confirm"]')
        if confirm.count() != 1:
            return False
        confirm.click(timeout=4000)
    # Deleting a menu item is not proof of server deletion. Confirm that the
    # exact owned conversation row disappears from the site's refreshed list.
    try:
        row.wait_for(state="detached", timeout=8000)
    except Exception:
        return False
    return True
