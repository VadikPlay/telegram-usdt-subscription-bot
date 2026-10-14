from unittest.mock import AsyncMock, MagicMock
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from gatekit.config import Settings
from gatekit.db.base import Base
from gatekit.db.models import Membership, MembershipState, SeenAd, User, utcnow
from gatekit.services.scraper_999 import ScrapedAd, ensure_sort_by_date, parse_ads_page
from gatekit.workers.scraper_worker import ScraperWorker

SAMPLE_999_HTML = """
<!DOCTYPE html>
<html>
<body>
<div class="ads-list-photo">
  <div class="ads-list-photo-item" data-ad-id="88123456">
    <a class="ads-list-photo-item-title" href="/ru/88123456">
      Apartament 2 camere Botanica Chisinau
    </a>
    <div class="ads-list-photo-item-price">65 000 €</div>
  </div>

  <div class="ads-list-photo-item" data-ad-id="88123457">
    <a class="ads-list-photo-item-title" href="/ro/booster/88123457">
      BMW 5 Series 2018 xDrive
    </a>
    <div class="ads-list-photo-item-price">22 500 EUR</div>
  </div>

  <div class="ads-list-photo-item" data-ad-id="88123458">
    <a href="/ru/88123458">
      Servicii Reparatie Apartamente
    </a>
    <span class="price">Договорная</span>
  </div>
</div>
</body>
</html>
"""


def test_parse_ads_page_standard_cards():
    ads = parse_ads_page(SAMPLE_999_HTML)
    assert len(ads) == 3

    assert ads[0].id == "88123456"
    assert ads[0].title == "Apartament 2 camere Botanica Chisinau"
    assert ads[0].price == "65 000 €"
    assert ads[0].url == "https://999.md/ru/88123456"

    assert ads[1].id == "88123457"
    assert ads[1].title == "BMW 5 Series 2018 xDrive"
    assert ads[1].price == "22 500 EUR"
    assert ads[1].url == "https://999.md/ro/booster/88123457"

    assert ads[2].id == "88123458"
    assert "Reparatie" in ads[2].title
    assert ads[2].price == "Договорная"


def test_parse_ads_page_deduplication():
    duplicate_html = """
    <div class="ads-list-photo-item" data-ad-id="10001">
      <a href="/ru/10001">Item 1</a>
      <div class="price">100 MDL</div>
    </div>
    <div class="ads-list-photo-item" data-ad-id="10001">
      <a href="/ru/10001">Item 1 duplicate</a>
      <div class="price">100 MDL</div>
    </div>
    """
    ads = parse_ads_page(duplicate_html)
    assert len(ads) == 1
    assert ads[0].id == "10001"


def test_parse_ads_page_unstructured_links():
    unstructured_html = """
    <div>
      <a href="/ru/99123456">iPhone 15 Pro Max 256GB</a>
      <span>Price: 18000 MDL</span>
    </div>
    """
    ads = parse_ads_page(unstructured_html)
    assert len(ads) == 1
    assert ads[0].id == "99123456"
    assert "iPhone 15" in ads[0].title
    assert ads[0].url == "https://999.md/ru/99123456"


def test_parse_ads_page_empty_and_whitespace():
    assert parse_ads_page("") == []
    assert parse_ads_page("   \n\t  ") == []
    assert parse_ads_page("<html><body><p>No ads here</p></body></html>") == []


@pytest.mark.asyncio
async def test_scraper_worker_tick_and_broadcast():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    now = utcnow()
    async with sessionmaker() as session:
        # User 1: Active subscriber
        u1 = User(id=111, username="active_user", lang="ru")
        m1 = Membership(
            user_id=111,
            plan_code="trial",
            state=MembershipState.ACTIVE,
            expires_at=now.replace(year=now.year + 1),
        )
        # User 2: Expired member (should NOT receive ads)
        u2 = User(id=222, username="expired_user", lang="en")
        m2 = Membership(
            user_id=222,
            plan_code="month",
            state=MembershipState.REVOKED,
            expires_at=now.replace(year=now.year - 1),
        )
        session.add_all([u1, m1, u2, m2])
        await session.commit()

    bot = AsyncMock()
    bot.send_message = AsyncMock(return_value=True)

    settings = MagicMock(spec=Settings)
    settings.scraper_url = "https://999.md/ru/list/real-estate/apartments-and-rooms"
    settings.scraper_interval_seconds = 30
    settings.default_lang = "ru"

    mock_client = AsyncMock()
    # Mock response returning sample HTML
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = SAMPLE_999_HTML
    mock_response.url = "https://999.md/ru/list/real-estate/apartments-and-rooms"
    mock_client.get = AsyncMock(return_value=mock_response)

    worker = ScraperWorker(
        bot=bot,
        settings=settings,
        sessionmaker=sessionmaker,
        client=mock_client,
    )

    # 1. First tick: should find 3 ads, persist them, and alert active user 111
    summary1 = await worker.tick()
    assert summary1["scraped"] == 3
    assert summary1["new"] == 3
    assert summary1["sent"] == 3  # sent 3 ads to user 111

    # Verify messages were sent to user 111 with inline keyboard
    assert bot.send_message.call_count == 3
    first_call_args = bot.send_message.call_args_list[0]
    assert first_call_args.kwargs["chat_id"] == 111
    assert "999.md" in first_call_args.kwargs["text"]
    assert first_call_args.kwargs["reply_markup"].inline_keyboard[0][0].url.startswith("https://999.md")

    # Verify database has 3 seen ads
    async with sessionmaker() as session:
        seen = (await session.scalars(select(SeenAd))).all()
        assert len(seen) == 3
        assert {s.id for s in seen} == {"88123456", "88123457", "88123458"}

    # 2. Second tick: same HTML -> 0 new ads, 0 messages sent (deduplication!)
    bot.send_message.reset_mock()
    summary2 = await worker.tick()
    assert summary2["scraped"] == 3
    assert summary2["new"] == 0
    assert summary2["sent"] == 0
    assert bot.send_message.call_count == 0

    await engine.dispose()


def test_parse_ads_page_ignores_external_links():
    html_with_external = """
    <div>
      <a href="https://itunes.apple.com/md/app/999.md-anunturi-online-in/id927359718?mt=8">App Store</a>
      <a href="https://www.instagram.com/999.md/">Instagram</a>
      <a href="/ru/98765432">Real 999 Apartment</a>
    </div>
    """
    ads = parse_ads_page(html_with_external)
    assert len(ads) == 1
    assert ads[0].id == "98765432"
    assert ads[0].url == "https://999.md/ru/98765432"


@pytest.mark.asyncio
async def test_send_ad_alert_escapes_html():
    bot = AsyncMock()
    bot.send_message = AsyncMock(return_value=True)
    settings = MagicMock(spec=Settings)
    settings.default_lang = "ru"

    worker = ScraperWorker(
        bot=bot,
        settings=settings,
        sessionmaker=MagicMock(),
    )

    ad = ScrapedAd(
        id="77112233",
        title="Apartament < 50 000€ & garaj >",
        price="45 000 € & bonus",
        url="https://999.md/ru/77112233",
    )

    sent = await worker._send_ad_alert(user_id=123, ad=ad, lang="ru")
    assert sent is True
    assert bot.send_message.call_count == 1
    sent_text = bot.send_message.call_args.kwargs["text"]
    # Check that <, >, & were properly escaped to HTML entities
    assert "&lt; 50 000€ &amp; garaj &gt;" in sent_text
    assert "45 000 € &amp; bonus" in sent_text
    assert "< 50" not in sent_text


@pytest.mark.asyncio
async def test_scraper_worker_handles_in_grace_and_blocked_subscribers():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    now = utcnow()
    async with sessionmaker() as session:
        # User 1: In grace period (expired 1 hour ago, but within 2-day grace period) -> SHOULD get ad
        u1 = User(id=301, username="grace_user", lang="ru", is_blocked=False)
        m1 = Membership(
            user_id=301,
            plan_code="month",
            state=MembershipState.IN_GRACE,
            expires_at=now.replace(hour=now.hour - 1 if now.hour > 0 else 0),
        )
        # User 2: Active subscriber but is_blocked=True -> SHOULD NOT get ad
        u2 = User(id=302, username="blocked_user", lang="ru", is_blocked=True)
        m2 = Membership(
            user_id=302,
            plan_code="month",
            state=MembershipState.ACTIVE,
            expires_at=now.replace(year=now.year + 1),
        )
        session.add_all([u1, m1, u2, m2])
        await session.commit()

    bot = AsyncMock()
    bot.send_message = AsyncMock(return_value=True)

    settings = MagicMock(spec=Settings)
    settings.scraper_url = "https://999.md/ru/list/real-estate/apartments-and-rooms"
    settings.scraper_interval_seconds = 30
    settings.grace_period_days = 2
    settings.default_lang = "ru"

    mock_client = AsyncMock()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = '<div class="ads-list-photo-item" data-ad-id="99112233"><a href="/ru/99112233">Test Ad</a><div class="price">100 MDL</div></div>'
    mock_response.url = settings.scraper_url
    mock_client.get = AsyncMock(return_value=mock_response)

    worker = ScraperWorker(
        bot=bot,
        settings=settings,
        sessionmaker=sessionmaker,
        client=mock_client,
    )

    summary = await worker.tick()
    assert summary["new"] == 1
    # Only user 301 (in grace) received the ad; user 302 (blocked) was excluded
    assert summary["sent"] == 1
    assert bot.send_message.call_count == 1
    assert bot.send_message.call_args.kwargs["chat_id"] == 301

    await engine.dispose()


def test_ensure_sort_by_date():
    url_without_sort = "https://999.md/ru/list/real-estate/apartments-and-rooms"
    sorted_url = ensure_sort_by_date(url_without_sort)
    assert "sort_type=date_desc" in sorted_url
    assert "applied=1" in sorted_url

    url_already_sorted = "https://999.md/ru/list/transport/cars?sort_type=date_desc"
    assert ensure_sort_by_date(url_already_sorted) == url_already_sorted

