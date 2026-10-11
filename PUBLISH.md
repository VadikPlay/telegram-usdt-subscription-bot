# Как выложить это на GitHub (5 минут)

Файл для тебя, а не для клиентов — перед публикацией удали его или добавь в `.gitignore`.

## 1. Подставь свои данные вместо заглушек

В коде три заглушки. Пока они на месте, ссылки в README и на лендинге ведут в никуда,
а блок «найми автора» не работает — то есть весь смысл входящего потока теряется.

```bash
cd telegram-usdt-subscription-bot

# твой GitHub-логин
grep -rl 'YOUR_GITHUB_USERNAME' . --exclude-dir=.git --exclude-dir=.venv \
  | xargs sed -i 's/YOUR_GITHUB_USERNAME/твой_логин/g'

# твой Telegram без @
grep -rl 'YOUR_TELEGRAM' . --exclude-dir=.git --exclude-dir=.venv \
  | xargs sed -i 's/YOUR_TELEGRAM/твой_юзернейм/g'

# проверь, что заглушек не осталось
grep -rn 'YOUR_GITHUB_USERNAME\|YOUR_TELEGRAM' . --exclude-dir=.git --exclude-dir=.venv || echo "чисто"
```

## 2. Создай репозиторий и запушь

Имя репозитория менять не советую: `telegram-usdt-subscription-bot` — это то, что люди
буквально вбивают в поиск GitHub и Google. Бренд Gatekit живёт внутри, в названии
продукта.

```bash
git init
git add .
git commit -m "Gatekit 1.0.0 — non-custodial Telegram subscription bot (USDT TRC20 / TON)"
git branch -M main
git remote add origin https://github.com/твой_логин/telegram-usdt-subscription-bot.git
git push -u origin main
```

Если репозиторий ещё не создан на GitHub и стоит `gh`:

```bash
gh repo create telegram-usdt-subscription-bot --public --source=. --push \
  --description "Self-hosted Telegram paid-subscription bot. USDT (TRC20) and TON straight to your own wallet. Non-custodial, MIT."
```

## 3. Настрой страницу репозитория

То, что решает, кликнет незнакомец или уйдёт:

- **Description**: `Self-hosted Telegram paid-subscription bot — USDT (TRC20) & TON paid directly to your own wallet. Non-custodial, MIT.`
- **Topics** (жми шестерёнку рядом с About): `telegram-bot` `aiogram` `usdt` `trc20` `tron` `ton` `subscriptions` `crypto-payments` `self-hosted` `python` `paid-channel` `non-custodial`
- **Website**: ссылка на лендинг, когда поднимешь
- Включи **Issues**, выключи Wiki и Projects — пусто выглядит хуже, чем отсутствует

## 4. Подними лендинг

Два варианта, оба бесплатные:

**GitHub Pages** — в настройках репозитория Pages → Source: `main`, папка `/landing`.
Получишь `https://твой_логин.github.io/telegram-usdt-subscription-bot/`.

**Свой домен** — у тебя уже есть VPS и домен:

```bash
scp landing/index.html user@сервер:/var/www/gatekit/index.html
```

и отдай статику nginx'ом на поддомене.

## 5. Что сделать до первой заявки

Клиент на маркетплейсе первым делом жмёт на ссылку с демо. Если там пусто — заявка
мертва.

- [ ] Заглушки заменены, ссылки в README открываются
- [ ] `python scripts/selftest.py` проходит на чистой машине
- [ ] Демо-бот поднят на твоём VPS, `/start` отвечает, тариф показывается
- [ ] Прогон на тестнете Nile: счёт → оплата → инвайт → вход в канал
- [ ] Записан короткий GIF работы (покажи: /start → счёт → подтверждение → ссылка),
      положен в README вместо текстового описания потока
- [ ] Первый релиз помечен тегом: `git tag v1.0.0 && git push --tags`

GIF важнее, чем кажется: он единственное доказательство, которое читают до кода.
