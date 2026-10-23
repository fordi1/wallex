import os
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

output_dir = Path("docs/images")
output_dir.mkdir(parents=True, exist_ok=True)

url = "https://pishro.liara.run"

print(f"Connecting to {url}...")
with sync_playwright() as p:
    browser = p.chromium.launch(channel="msedge", headless=True)
    
    # High-res Desktop View
    context = browser.new_context(
        viewport={"width": 1600, "height": 1000},
        device_scale_factor=2,
    )
    page = context.new_page()
    page.goto(url, wait_until="domcontentloaded", timeout=20000)
    
    # Wait for the client to poll /api/status and render the UI
    print("Waiting for dashboard data to populate...")
    page.wait_for_timeout(4500)
    
    # Overview Screenshot (Viewport)
    overview_path = output_dir / "dashboard_overview.png"
    page.screenshot(path=str(overview_path))
    print(f"Saved: {overview_path}")
    
    # Full Page Screenshot (Dark Mode)
    full_path = output_dir / "dashboard_full.png"
    page.screenshot(path=str(full_path), full_page=True)
    print(f"Saved: {full_path}")
    
    # Working Orders Panel
    orders_panel = page.locator(".panel:has(#ordersBody)")
    if orders_panel.count() > 0:
        orders_path = output_dir / "dashboard_orders.png"
        orders_panel.first.screenshot(path=str(orders_path))
        print(f"Saved: {orders_path}")
        
    # Reversion Radar Panel
    radar_panel = page.locator(".panel:has(#radarBody)")
    if radar_panel.count() > 0:
        radar_path = output_dir / "dashboard_radar.png"
        radar_panel.first.screenshot(path=str(radar_path))
        print(f"Saved: {radar_path}")
        
    # Algorithmic Decisions & Policy Panel
    decisions_panel = page.locator(".dash-grid > div:last-child")
    if decisions_panel.count() > 0:
        decisions_path = output_dir / "dashboard_decisions.png"
        decisions_panel.first.screenshot(path=str(decisions_path))
        print(f"Saved: {decisions_path}")
        
    # Light Mode Screenshot
    theme_btn = page.locator("button[title='تغییر تم']")
    if theme_btn.count() > 0:
        theme_btn.click()
        page.wait_for_timeout(1000)
        light_path = output_dir / "dashboard_light.png"
        page.screenshot(path=str(light_path), full_page=True)
        print(f"Saved: {light_path}")
        
    browser.close()

print("All screenshots generated successfully!")
