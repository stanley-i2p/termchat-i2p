## TermChatI2P

TermChatI2P is a terminal-based private messenger designed for one-to-one communication over the I2P network.  
It supports both **live encrypted chat** and **offline messaging**, while keeping the protocol compact and operationally simple.

The application has two modes: **transient mode** for short-lived live sessions, and **persistent mode** for long-term trusted peers with saved identity and offline support.

Live messages use an internal framed protocol and an additional end-to-end encryption layer on top of I2P transport.

Persistent mode can lock a profile to a single peer, store offline messaging state, and exchange queued messages through minimal deaddrop servers.  

Offline delivery uses opaque encrypted blobs and rotating per-message lookup keys, so the storage layer learns very little.

Peer trust in persistent mode is strengthened with **TOFU (Trust On First Use)** by pinning the peer’s full I2P destination identity for future verification.  

The design emphasizes compartmentalization: live chat, offline delivery, persistent trust, and transient sessions are intentionally separated.  

As a result, the messenger aims to provide strong privacy, low metadata exposure, and practical offline capability without relying on heavy server-side logic.

## Project Status

- This project is being developed in **multiple phases**, and a number of features and refinements are still planned for later stages.
- At the current stage, the **core architecture is already in place**, including most of the important **security mechanisms** and the full **offline messaging foundation**.
- The main work that remains is largely around **interface beautification**, usability polish, and smaller supporting features rather than the core privacy model.
- Additional work is being done for offline replication as well as for offline server lists exchange protocol (natual diffusion model).
- After broader real-world testing and possible **security review / audit**, we are considering a future **rewrite in C++**.
- In the longer term, the Python version is also expected to **move away from `libi2p` entirely** in favor of a cleaner and more controlled implementation path.






## TermchatI2P: Децентрализованный защищенный мессенджер

TermchatI2P — это консольный (TUI) мессенджер, работающий через анонимную сеть **I2P (Invisible Internet Project)**. Проект ориентирован на максимальную приватность, исключая центральные сервера и метаданные.

![TermchatI2P](chat.png)
![TermchatI2P](chat2.png)

## 🚀 Быстрый старт

### Предварительные требования
1.  **I2P Роутер:** На вашем компьютере должен быть запущен I2P роутер (Java I2P или i2pd).
2.  **SAM интерфейс:** Убедитесь, что в настройках роутера включен протокол SAM (обычно порт `7656`).
3.  **Python version > 3.9, 3.14 preferred** и установленные зависимости:
    ```bash
    pip install i2plib textual rich
    ```

### 🐍 Настройка окружения (Python 3.14 + venv)

Для изоляции зависимостей и корректной работы мессенджера рекомендуется использовать виртуальное окружение.

#### Установка Python 3.14 (если не установлен)
1. Установка uv
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```
2. Создание окружения с ПРАВИЛЬНОЙ версией одной командой
```bash
uv venv --python 3.14 i2p_env
```
3. Активация

```bash
source i2p_env/bin/activate
```



### Запуск
Для запуска мессенджера используйте команду:
```bash
python chat.py [имя_профиля]
```

Если имя_профиля не указано, приложение запустится в Transient (временном) режиме — ваш адрес будет меняться при каждом перезапуске.
Если указать имя (например, python chat.py alice), создастся файл alice.dat, который сохранит ваш постоянный адрес I2P.

🛠 Управление в приложении

    Связь с контактом: Введите /connect <адрес.b32.i2p> в поле ввода.
    Быстрое подключение: Если в файле профиля (вторая строка .dat файла) сохранен адрес друга, введите /connect без аргументов.
    Выход: Нажмите Ctrl+Q или просто q.

### 🔒 Анализ безопасности и сравнение

TermchatI2P спроектирован с упором на архитектуру **Zero-Trust** (нулевое доверие). Ниже приведено сравнение с популярными защищенными мессенджерами.


| Функция | Telegram (Secret) | Signal | TermchatI2P (I2P) |
| :--- | :---: | :---: | :---: |
| **Центральный сервер** | Да | Да | **Нет (P2P)** |
| **Скрытие IP-адреса** | Нет | Нет | **Да (По умолчанию)** |
| **Привязка к номеру** | Да | Да | **Нет (Анонимно)** |
| **Метаданные** | Хранятся на сервере | Минимум | **Отсутствуют** |
| **Устойчивость к цензуре** | Высокая | Высокая | **Абсолютная** |

---

### 🛡️ Изолированный подход (Compartment Approach)

TermchatI2P реализует архитектуру **"один пользователь — один ключ"**, также известную как метод разделения (compartmentalization). В отличие от традиционных мессенджеров, здесь безопасность строится не вокруг платформы, а вокруг каждой отдельной сессии.

#### Почему это превосходит другие архитектуры?

1. **Полная изоляция (Compartmentalization):**
   В обычных мессенджерах (Telegram, Signal) ваш аккаунт — это единая точка отказа. Если скомпрометирован номер телефона или доступ к серверу, злоумышленник видит все ваши контакты и метаданные. В TermchatI2P вы можете иметь 10 разных профилей (`.dat` файлов) для 10 разных собеседников. Компрометация одного ключа никак не влияет на безопасность остальных.

2. **Отсутствие глобального идентификатора:**
   Здесь нет общего реестра пользователей. Ваша личность существует только в рамках пары ключей. Это исключает возможность "Correlation Attacks" (атак через сопоставление данных), так как внешнему наблюдателю невозможно доказать, что два разных адреса принадлежат одному и тому же человеку.

3. **Локальный контроль над "Доверием":**
   В этой архитектуре сервер не является "доверенной стороной", потому что его просто не существует. Вы сами решаете, кому разрешить подключение (через `stored_peer` в файле профиля). Это превращает ваше устройство в неприступную крепость, которая игнорирует любые запросы извне, кроме тех, что подписаны доверенным ключом.

4. **Защита от массовой слежки:**
   Традиционные системы безопасности (даже с E2EE) уязвимы к анализу графа связей. I2PChat разбивает этот граф на мелкие, несвязанные сегменты. Даже обладая неограниченными ресурсами, спецслужбы не могут построить карту ваших контактов, так как каждый профиль — это "цифровой призрак".

> **Итог:** Это не просто мессенджер, а инструмент для создания независимых каналов связи, где каждая пара собеседников живет в своей собственной зашифрованной вселенной.


## Основные функции

### Обмен текстовыми сообщениями

Пользователи могут отправлять и получать текстовые сообщения в
реальном времени. Сообщения отображаются в виде «пузырей» (message
bubbles) в терминальном интерфейсе.

Каждое сообщение имеет:

- уникальный идентификатор (`MSG_ID`)
- отметку времени (UTC)
- подтверждение доставки

---

### Подтверждение доставки

После получения сообщения клиент отправляет подтверждение доставки.

Это позволяет отправителю увидеть индикатор доставки рядом с
сообщением в интерфейсе.

---

### Передача изображений

Клиенты могут отправлять изображения напрямую через соединение.

Передача выполняется поэтапно:

1. отправка заголовка изображения
2. передача данных по частям (chunks)
3. завершение передачи

После получения изображение автоматически сохраняется и отображается
в терминале.

Режимы отображения:

- нативный рендеринг терминала (если поддерживается)
- ASCII/Braille рендеринг для обычных терминалов

---

### Передача файлов

Поддерживается отправка произвольных файлов между пользователями.

Файлы передаются по частям, что позволяет передавать большие объёмы
данных без перегрузки соединения.

Полученные файлы сохраняются в локальной директории клиента.

---

### Сквозное шифрование (E2E)

Все пользовательские данные могут передаваться с использованием
сквозного шифрования.

Это означает, что:

- сообщения шифруются на стороне отправителя
- расшифровываются только на стороне получателя
- промежуточные узлы сети не имеют доступа к содержимому

---

### Используемые алгоритмы

Для реализации E2E используются следующие криптографические примитивы:

| Назначение | Алгоритм |
|-------------|-----------|
| Обмен ключами | X25519 |
| Шифрование | ChaCha20-Poly1305 |
| Проверка целостности | Poly1305 (в составе AEAD) |
| Генерация ключей | HKDF |

---

### Локальное хранилище

Приложение использует изолированную директорию пользователя:
```bash 
~/.termchat-i2p/
```

В ней хранятся:

- профили и ключи пользователя
- полученные изображения
- полученные файлы
- служебные данные приложения

---

### Архитектурные особенности

Протокол разработан с учётом будущих возможностей:

- оффлайн сообщений
- распределённых «dead-drop» хранилищ
- репликации данных между узлами
- расширения типов сообщений

Это позволяет постепенно развивать систему без изменения базового
протокола.


## 📝 Инструкция по использованию

*   Если **имя_профиля** не указано, приложение запустится в **Transient** (временном) режиме — ваш адрес будет меняться при каждом перезапуске.
*   Если указать имя (например, `python chat.py alice`), создастся файл `alice.dat`, который сохранит ваш постоянный адрес I2P.

### 🛠 Управление в приложении

*   **Связь с контактом:** Введите `/connect <адрес.b32.i2p>` в поле ввода.
*   **Быстрое подключение:** Если в файле профиля (вторая строка `.dat` файла) сохранен адрес друга, введите `/connect` без аргументов.
*   **Выход:** Нажмите `Ctrl+Q` или просто `q`.

## Протокол обмена сообщениями

Приложение использует лёгкий бинарный протокол кадров (framed protocol) для
надёжной передачи данных по постоянному потоку (например, через I2P SAM).

Протокол предназначен для:

- обмена текстовыми сообщениями
- передачи файлов и изображений
- уведомлений о доставке
- устойчивости к рассинхронизации потока
- дальнейшего расширения (например, офлайн-сообщений)

---

### Структура кадра

Каждое сообщение передаётся как бинарный кадр:

```
MAGIC | VERSION | TYPE | MSG_ID | LEN | PAYLOAD
```


### Размеры полей:

| Поле | Размер | Описание |
|-----|------|-------------|
| MAGIC | 4 байта | Маркер кадра для синхронизации (`0x89 49 32 50`) |
| VERSION | 1 байт | Версия протокола |
| TYPE | 1 байт | Тип сообщения |
| MSG_ID | 8 байт | Уникальный идентификатор сообщения |
| LEN | 4 байта | Размер полезной нагрузки |
| PAYLOAD | переменный | Содержимое сообщения |

---

### Сообщения

| Тип | Описание |
|----|-----------|
| `U` | Текстовое сообщение пользователя |
| `D` | Уведомление о доставке |

---

### Служебные сообщения

| Тип | Описание |
|----|-----------|
| `P` | Ping |
| `O` | Pong |
| `S` | Сигналы управления |

Пример сигнала:


```bash
__SIGNAL__:QUIT
__SIGNAL__:TYPING
```


---

### Передача файлов

Передача файлов выполняется в три этапа:

| Тип | Описание |
|----|-----------|
| `F` | Начало передачи файла (`имя|размер`) |
| `C` | Блок данных (base64) |
| `E` | Завершение передачи |

---

### Передача изображений

Изображения передаются аналогично файлам:

| Тип | Описание |
|----|-----------|
| `M` | Начало передачи изображения |
| `C` | Блок данных изображения |
| `I` | Завершение передачи |

---

### Устойчивость к рассинхронизации

Каждый кадр начинается с маркера `MAGIC`, что позволяет получателю
восстановить синхронизацию потока в случае повреждения данных.

---

### Транспорт

Протокол работает поверх потокового соединения (TCP-подобного),
например через сеть I2P.

Надёжность доставки и порядок сообщений обеспечиваются
транспортным уровнем.


# Offline Messaging Architecture

## Overview

This chat has two operating modes:

- **Transient mode**
  - live 1:1 chat only
  - no offline deaddrop messaging by default

- **Persistent mode**
  - identity is stored locally
  - peer is locked to a saved `.b32.i2p`
  - offline messaging is enabled
  - offline state is stored per locked peer

All communication runs over **I2P**.

---

# Offline Messaging Architecture

## Overview

This chat has two operating modes:

- **Transient mode**
  - live 1:1 chat only
  - no offline deaddrop messaging by default

- **Persistent mode**
  - identity is stored locally
  - peer is locked to a saved `.b32.i2p`
  - offline messaging is enabled
  - offline state is stored per locked peer

All communication runs over **I2P**.

---

## Live Protocol

The inner application frame is:

```bash
MAGIC | VERSION | TYPE | MSG_ID | LEN | PAYLOAD
```
This frame is used for normal live 1:1 communication and is also the payload carried inside offline blobs.

### Offline Blob Format

Offline blobs are intentionally inert and contain no metadata:

```bash
nonce | enc(nonce, frame)
```
Where:
    - frame is the normal inner app protocol frame
    - nonce is per-message
    - encrypted blob contains no sender, recipient, timestamp, or routing metadata

### Deaddrop Model

Offline delivery uses a deaddrop server with a very small protocol:

```bash
PUT <key> <size>
GET <key>
```
Properties:
    - one key = one blob
    - server stores opaque bytes only
    - server does not parse chat protocol
    - overwrite is refused
    - server may return EXISTS
    - expired blobs are removed by TTL/GC

### Offline Keying

Each offline message uses a derived per-message key.

Conceptually:

```bash
key_i = KDF(offline_shared_secret, peer identities, direction, index)
```
Properties:
    - one derived key per message
    - sender advances send index
    - receiver searches a bounded receive window
    - no mailbox-style multi-message bucket
    - stronger compartmentalization and less linkability

### Offline Receive Flow

In persistent mode, when offline runtime is active:
* compute receive key window
* GET each candidate key
* if blob exists:
  - hash-check duplicate
  - decrypt blob
  - recover inner frame
  - parse normal app protocol frame
  - process through normal frame handler
  - mark receive index consumed
  - advance receive base
  - persist updated offline state

### Offline Send Flow

If no live connection exists and offline mode is active:
* build normal inner frame
```bash
MAGIC | VERSION | TYPE | MSG_ID | LEN | PAYLOAD
```
* wrap as offline blob
* nonce | enc(nonce, frame)
* derive next send key
* PUT blob to deaddrop
* increment send index
* persist updated offline state

### Offline Runtime Rules

Persistent mode
* offline messaging enabled
* locked peer required
* offline secret stored per locked peer
* send/receive counters stored per locked peer
* receive window state stored per locked peer

Transient mode
* live chat only
* no offline deaddrop by default

### Offline State

Per locked peer, persistent mode stores:
* offline_shared_secret
* drop_send_index
* drop_recv_base
* drop_window
* consumed receive indexes

This allows restart-safe offline sending and receiving.

### Offline Secret Exchange

Offline messaging uses a dedicated shared secret separate from live session traffic.

Current design:
* secret is exchanged over an already encrypted live session
* stored per locked peer
* deterministic initiator rule avoids race
* later bound by TOFU

### TOFU

Persistent mode uses TOFU for peer authenticity.

Pinned item:
* peer full I2P destination identity (base64 destination / fingerprint)

Behavior:
* first trusted save pins the peer identity
* future live sessions must match the pinned identity
* mismatch is blocked

This binds:
* live chat trust
* offline secret
* offline counters/state

to the same persistent peer identity.

### Deaddrop Server Retention

Server retention is TTL-based:
* blobs remain stored for a configured time
* expired blobs are treated as missing
* background GC removes expired files
* no explicit delete/ack is required for basic operation

Because keys rotate per message, old messages do not normally reappear unless client state is lost or rolled back.

### Current Operational Model

* live connected → normal real-time chat
* persistent mode + locked peer + offline mode + no live chat → offline send/receive through deaddrop
* startup in persistent mode → load identity, locked peer, offline state, start listener, start deaddrop runtime when appropriate

## Security Summary

### Strong points

* all traffic over I2P
* live content encrypted end-to-end
* offline blobs contain no metadata
* one key per message
* rotating derived keys with receive window search
* transient I2P access can be used for deaddrop operations
* server sees only opaque keys/blobs
* persistent/transient split reduces unnecessary residue
* TOFU binds long-term peer identity

### Residual limits

* timing and traffic-pattern leakage still exist - (irrelevant on I2P) 
* blob size may still reveal coarse information unless padded
* local endpoint compromise defeats all protocol protections - (operational discipline)

### Overall

This architecture provides strong privacy, strong compartmentalization, and strong peer authenticity once TOFU is enforced, while keeping the offline layer minimal and metadata-poor.
