async def _capture_full_market_text(self, page: Page, steps: int = 10) -> str:
        """SportyBet's markets panel is virtualized — only what's scrolled
        into view exists in the DOM at any instant. Scroll step by step
        and accumulate every unique line seen along the way, so Groq gets
        the FULL market list (1H/2H/FT Over-Under lines included), not
        just whatever happened to be on screen at one instant.
        Scrolls back to the top afterward so the later click-search phase
        starts from the top of the page again."""
        await self._click_all_tab(page)

        seen_lines: List[str] = []
        seen_set = set()

        async def snapshot():
            # Try to get text from the specific market container first
            try:
                # SportyBet uses a virtualized list - try multiple selectors
                selectors = [
                    "[class*='market-list']",
                    "[class*='MarketList']", 
                    "[class*='markets']",
                    "[class*='Markets']",
                    "[data-testid*='market']",
                    "[class*='event-market']",
                    "[class*='eventMarket']",
                    "main > div > div",  # Common structure
                ]
                
                for sel in selectors:
                    try:
                        loc = page.locator(sel)
                        count = await loc.count()
                        if count > 0:
                            # Get text from all matching containers
                            texts = []
                            for i in range(min(count, 5)):
                                try:
                                    t = await loc.nth(i).inner_text(timeout=1000)
                                    if t and len(t) > 50:  # Only substantial text
                                        texts.append(t)
                                except:
                                    pass
                            if texts:
                                return "\n".join(texts)
                    except:
                        pass
                
                # Fallback to body if no container found
                t = await page.inner_text("body", timeout=2500)
                return t or ""
            except Exception:
                return ""

        # Take initial snapshot
        t = await snapshot()
        for line in (t or "").split("\n"):
            line = line.strip()
            if line and line not in seen_set:
                seen_set.add(line)
                seen_lines.append(line)

        # Scroll and capture
        for i in range(steps):
            try:
                # Try to scroll the specific market container if possible
                try:
                    # Find scrollable container
                    await page.evaluate("""() => {
                        const containers = document.querySelectorAll('div[class*="market"], div[class*="Market"], [class*="scroll"], [class*="virtual"]');
                        for (const c of containers) {
                            if (c.scrollHeight > c.clientHeight) {
                                c.scrollBy(0, 400);
                                return true;
                            }
                        }
                        return false;
                    }""")
                except:
                    pass
                
                # Fallback to mouse wheel
                await page.mouse.wheel(0, 500 + i * 30)
                await asyncio.sleep(0.4)  # Longer wait for lazy loading
                
            except Exception:
                break
            
            # Capture after scroll
            t = await snapshot()
            new_count = 0
            for line in (t or "").split("\n"):
                line = line.strip()
                if line and line not in seen_set:
                    seen_set.add(line)
                    seen_lines.append(line)
                    new_count += 1
            
            logger.debug(f"[SCROLL] Step {i+1}: +{new_count} new lines")

        # Scroll back to top
        try:
            await page.evaluate("window.scrollTo(0, 0)")
            # Also scroll market container to top
            await page.evaluate("""() => {
                const containers = document.querySelectorAll('div[class*="market"], div[class*="Market"], [class*="scroll"]');
                for (const c of containers) c.scrollTop = 0;
            }""")
        except Exception:
            pass

        result = "\n".join(seen_lines)
        logger.info(f"[CAPTURE] Total unique lines: {len(seen_lines)}, length: {len(result)}")
        return result[:9000]
