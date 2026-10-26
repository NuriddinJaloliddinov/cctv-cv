/* UI-only translations. API values and saved camera/person data are never changed. */
(() => {
  const translations = {
    ru: {
      'Overview': 'Обзор', 'Cameras': 'Камеры', 'Enrollment': 'Регистрация',
      'Attendance': 'Посещаемость', 'Customers': 'Посетители', 'Server': 'Сервер',
      'Training': 'Обучение', 'Telegram': 'Telegram', 'Accounts': 'Учётные записи',
      'Password': 'Пароль', 'Sign out': 'Выйти', 'Administrator': 'Администратор',
      'Read-only': 'Только просмотр', 'User': 'Пользователь',
      'Server online': 'Сервер доступен', 'Server offline': 'Сервер недоступен',
      'Refresh': 'Обновить', 'Delete': 'Удалить', 'Delete all': 'Удалить все',
      'Edit': 'Изменить', 'Save': 'Сохранить', 'Cancel': 'Отмена',
      'Close': 'Закрыть', 'Clear': 'Очистить', 'Remove': 'Убрать',
      'Add': 'Добавить', 'Enabled': 'Включено', 'Disabled': 'Выключено',
      'On': 'Вкл.', 'Off': 'Выкл.', 'Yes': 'Да', 'No': 'Нет',
      'Status': 'Статус', 'Camera': 'Камера', 'Name': 'Имя', 'Address': 'Адрес',
      'Person': 'Человек', 'Role': 'Роль', 'Today': 'Сегодня',
      'Last entrance': 'Последний вход', 'Last exit': 'Последний выход',
      'Inside': 'Внутри', 'Outside': 'Снаружи', 'No record': 'Нет записей',
      'Inside now': 'Сейчас внутри', 'Entrances today': 'Входов сегодня',
      'Exits today': 'Выходов сегодня', 'Enrolled people': 'Зарегистрированные люди',
      'Customers today': 'Посетителей сегодня', 'Customer exits today': 'Выходов посетителей сегодня',
      'Not confirmed events': 'Неподтверждённых событий', 'Cameras running': 'Работающих камер',
      'Recent events': 'Последние события', 'Not confirmed': 'Не подтверждено',
      'Confirmed': 'Подтверждено', 'Rejected': 'Отклонено', 'Failed': 'Ошибка',
      'Events': 'События', 'Recent attendance': 'Последние записи посещаемости',
      'Unassigned crossings': 'Неназначенные проходы', 'Not counted': 'Не учтено',
      'Entered': 'Вошёл', 'Left': 'Вышел', 'Customer': 'Посетитель',
      'On a call': 'Разговор по телефону', 'Looking at phone': 'Смотрит в телефон',
      'Phone use (model prediction)': 'Использование телефона (прогноз модели)',
      'No attendance records yet.': 'Записей посещаемости пока нет.',
      'No customer events yet.': 'Событий посетителей пока нет.',
      'No events yet.': 'Событий пока нет.',
      'No events waiting for confirmation.': 'Нет событий, ожидающих подтверждения.',
      'No unassigned crossings.': 'Неназначенных проходов нет.',
      'No enrolled people yet.': 'Зарегистрированных людей пока нет.',
      'No cameras yet. Click Discover or add a URL.': 'Камер пока нет. Запустите поиск или добавьте URL.',
      'Camera Setup': 'Настройка камер', 'Live Analytics Feed': 'Прямой видеопоток',
      'Detection controls': 'Управление обнаружением', 'Person detection': 'Обнаружение людей',
      'Phone-use detection': 'Обнаружение использования телефона',
      'Save detection settings': 'Сохранить настройки обнаружения',
      'Global · all cameras': 'Общие · все камеры',
      'Exclusive · selected cameras': 'Отдельные · выбранные камеры',
      'Count as Customer': 'Считать посетителем',
      'Filter people': 'Поиск людей', 'Filter by name or address': 'Поиск по имени или адресу',
      'Enroll Person': 'Зарегистрировать человека', '+ Enroll Person': '+ Зарегистрировать',
      'Enrolled People': 'Зарегистрированные люди',
      'Unrecognised face review': 'Проверка неизвестных лиц',
      'Capture unrecognised faces': 'Сохранять неизвестные лица',
      'Review & Label': 'Просмотр и разметка', 'Needs review': 'Требует проверки',
      'No phone use': 'Без использования телефона', 'No person': 'Нет человека',
      'Unclear': 'Неясно', 'Calling': 'Разговор', 'Looking': 'Смотрит',
      'Add missed cases': 'Добавить пропущенные случаи',
      'Train & validate': 'Обучение и проверка', 'Train candidate': 'Обучить модель',
      'Deploy validated candidate': 'Развернуть проверенную модель',
      'Roll back to original': 'Вернуться к исходной модели',
      'Automatic snapshot capture': 'Автоматическое сохранение снимков',
      'Enable automatic capture': 'Включить автоматическое сохранение',
      'Save capture settings': 'Сохранить настройки сохранения',
      'Delete image': 'Удалить изображение', 'Add to review': 'Добавить на проверку',
      'Camera role': 'Роль камеры', 'No role': 'Без роли',
      'Entrance · entered': 'Вход · вошёл', 'Exit · left': 'Выход · вышел',
      'Crossing direction': 'Направление прохода',
      'Save changes': 'Сохранить изменения', 'Edit camera': 'Изменить камеру',
      'Edit person': 'Изменить человека', 'Enroll': 'Зарегистрировать',
      'Add samples': 'Добавить образцы', 'Photos / videos': 'Фото / видео',
      'Username': 'Имя пользователя', 'Sign in': 'Войти', 'Signing in…': 'Вход…',
      'Sign in to the monitoring dashboard': 'Войти в панель мониторинга',
      'Enter your username and password.': 'Введите имя пользователя и пароль.',
      'Sign-in failed. Try again.': 'Не удалось войти. Попробуйте ещё раз.',
      'Invalid username or password.': 'Неверное имя пользователя или пароль.',
      'This account is disabled.': 'Эта учётная запись отключена.',
      'This account is locked after too many failed sign-in attempts. Ask an administrator to unlock it.': 'Учётная запись заблокирована после слишком многих неудачных попыток входа. Обратитесь к администратору.',
      'This account has been locked after too many failed sign-in attempts. Ask an administrator to unlock it.': 'Учётная запись заблокирована после слишком многих неудачных попыток входа. Обратитесь к администратору.',
      'Could not reach the server. Check your connection.': 'Не удаётся связаться с сервером. Проверьте соединение.',
      'Change password': 'Сменить пароль', 'Current password': 'Текущий пароль',
      'New password': 'Новый пароль', 'Confirm new password': 'Подтвердите новый пароль',
      'Update password': 'Обновить пароль', 'Account enabled': 'Учётная запись активна',
      '+ Add account': '+ Добавить учётную запись', 'Save account': 'Сохранить учётную запись',
      'Add account': 'Добавить учётную запись', 'Edit account': 'Изменить учётную запись',
      'Unlock': 'Разблокировать', 'Disable': 'Отключить', 'Enable': 'Включить',
      'Server capacity': 'Мощность сервера', 'Concurrent cameras': 'Одновременных камер',
      'Camera FPS': 'Частота кадров камеры', 'Stream width (pixels)': 'Ширина потока (пиксели)',
      'Stream height (pixels)': 'Высота потока (пиксели)',
      'People per frame at peak': 'Людей в кадре в пиковый момент',
      'Simultaneous live viewers': 'Одновременных зрителей',
      'Stream bitrate (Mbps/camera)': 'Битрейт потока (Мбит/с на камеру)',
      'Estimated snapshots (MB/day total)': 'Оценка объёма снимков (МБ/день)',
      'Retention (days)': 'Срок хранения (дни)',
      'Check this server (15 seconds)': 'Проверить сервер (15 секунд)',
      'What remains unverified': 'Что ещё не проверено', 'Bottlenecks': 'Узкие места',
      'This server': 'Этот сервер', 'Observed cameras': 'Наблюдаемые камеры',
      'Not yet verified': 'Пока не проверено',
      'Delete event': 'Удалить событие', 'Delete records': 'Удалить записи',
      'All customer events': 'Все события посетителей',
      'All cameras': 'Все камеры', 'All labels': 'Все метки',
      'Upload': 'Загрузить', 'Disconnect': 'Отключить',
      'Verify connection': 'Проверить соединение', 'Save settings': 'Сохранить настройки',
      'Not configured': 'Не настроено', 'Configured': 'Настроено',
      'Connection error': 'Ошибка соединения',
      'Source type': 'Тип источника', 'Default device': 'Устройство по умолчанию',
      'Default FPS': 'Частота кадров по умолчанию',
      'RTSP URL': 'RTSP URL', 'FPS limit': 'Ограничение FPS',
      'Activity log': 'Журнал действий', 'Check feeds with these credentials': 'Проверить потоки с этими данными',
      'Run CV Model': 'Запустить модель', 'Stop CV Model': 'Остановить модель',
      'Advanced scan options': 'Дополнительные параметры поиска',
      'Discover NVR & Camera': 'Найти регистратор и камеру',
      'Dashboard language': 'Язык панели',
      'Open menu': 'Открыть меню', 'Close menu': 'Закрыть меню',
      'Main navigation': 'Главная навигация',
    },
    uz: {
      'Overview': 'Umumiy ko‘rinish', 'Cameras': 'Kameralar', 'Enrollment': 'Ro‘yxatga olish',
      'Attendance': 'Davomat', 'Customers': 'Mijozlar', 'Server': 'Server',
      'Training': 'O‘qitish', 'Telegram': 'Telegram', 'Accounts': 'Hisoblar',
      'Password': 'Parol', 'Sign out': 'Chiqish', 'Administrator': 'Administrator',
      'Read-only': 'Faqat ko‘rish', 'User': 'Foydalanuvchi',
      'Server online': 'Server ishlayapti', 'Server offline': 'Server ishlamayapti',
      'Refresh': 'Yangilash', 'Delete': 'O‘chirish', 'Delete all': 'Barchasini o‘chirish',
      'Edit': 'Tahrirlash', 'Save': 'Saqlash', 'Cancel': 'Bekor qilish',
      'Close': 'Yopish', 'Clear': 'Tozalash', 'Remove': 'Olib tashlash',
      'Add': 'Qo‘shish', 'Enabled': 'Yoqilgan', 'Disabled': 'O‘chirilgan',
      'On': 'Yoniq', 'Off': 'O‘chiq', 'Yes': 'Ha', 'No': 'Yo‘q',
      'Status': 'Holat', 'Camera': 'Kamera', 'Name': 'Ism', 'Address': 'Manzil',
      'Person': 'Shaxs', 'Role': 'Rol', 'Today': 'Bugun',
      'Last entrance': 'Oxirgi kirish', 'Last exit': 'Oxirgi chiqish',
      'Inside': 'Ichkarida', 'Outside': 'Tashqarida', 'No record': 'Yozuv yo‘q',
      'Inside now': 'Hozir ichkarida', 'Entrances today': 'Bugungi kirishlar',
      'Exits today': 'Bugungi chiqishlar', 'Enrolled people': 'Ro‘yxatdagi shaxslar',
      'Customers today': 'Bugungi mijozlar', 'Customer exits today': 'Bugungi mijoz chiqishlari',
      'Not confirmed events': 'Tasdiqlanmagan hodisalar', 'Cameras running': 'Ishlayotgan kameralar',
      'Recent events': 'So‘nggi hodisalar', 'Not confirmed': 'Tasdiqlanmagan',
      'Confirmed': 'Tasdiqlangan', 'Rejected': 'Rad etilgan', 'Failed': 'Xato',
      'Events': 'Hodisalar', 'Recent attendance': 'So‘nggi davomat',
      'Unassigned crossings': 'Biriktirilmagan o‘tishlar', 'Not counted': 'Hisobga olinmagan',
      'Entered': 'Kirdi', 'Left': 'Chiqdi', 'Customer': 'Mijoz',
      'On a call': 'Telefonda gaplashmoqda', 'Looking at phone': 'Telefonga qaramoqda',
      'Phone use (model prediction)': 'Telefondan foydalanish (model taxmini)',
      'No attendance records yet.': 'Hozircha davomat yozuvlari yo‘q.',
      'No customer events yet.': 'Hozircha mijoz hodisalari yo‘q.',
      'No events yet.': 'Hozircha hodisalar yo‘q.',
      'No events waiting for confirmation.': 'Tasdiqlashni kutayotgan hodisalar yo‘q.',
      'No unassigned crossings.': 'Biriktirilmagan o‘tishlar yo‘q.',
      'No enrolled people yet.': 'Hozircha ro‘yxatga olinganlar yo‘q.',
      'No cameras yet. Click Discover or add a URL.': 'Kameralar yo‘q. Qidiruvni boshlang yoki URL qo‘shing.',
      'Camera Setup': 'Kamera sozlamalari', 'Live Analytics Feed': 'Jonli video oqimi',
      'Detection controls': 'Aniqlash boshqaruvi', 'Person detection': 'Odamlarni aniqlash',
      'Phone-use detection': 'Telefondan foydalanishni aniqlash',
      'Save detection settings': 'Aniqlash sozlamalarini saqlash',
      'Global · all cameras': 'Umumiy · barcha kameralar',
      'Exclusive · selected cameras': 'Alohida · tanlangan kameralar',
      'Count as Customer': 'Mijoz sifatida hisoblash',
      'Filter people': 'Shaxslarni qidirish', 'Filter by name or address': 'Ism yoki manzil bo‘yicha qidirish',
      'Enroll Person': 'Shaxsni ro‘yxatga olish', '+ Enroll Person': '+ Shaxs qo‘shish',
      'Enrolled People': 'Ro‘yxatdagi shaxslar',
      'Unrecognised face review': 'Noma’lum yuzlarni tekshirish',
      'Capture unrecognised faces': 'Noma’lum yuzlarni saqlash',
      'Review & Label': 'Ko‘rish va belgilash', 'Needs review': 'Tekshirish kerak',
      'No phone use': 'Telefon ishlatilmagan', 'No person': 'Odam yo‘q',
      'Unclear': 'Noaniq', 'Calling': 'Gaplashish', 'Looking': 'Qarash',
      'Add missed cases': 'O‘tkazib yuborilgan holatlarni qo‘shish',
      'Train & validate': 'O‘qitish va tekshirish', 'Train candidate': 'Modelni o‘qitish',
      'Deploy validated candidate': 'Tekshirilgan modelni qo‘llash',
      'Roll back to original': 'Asl modelga qaytish',
      'Automatic snapshot capture': 'Avtomatik suratga olish',
      'Enable automatic capture': 'Avtomatik suratga olishni yoqish',
      'Save capture settings': 'Suratga olish sozlamalarini saqlash',
      'Delete image': 'Rasmni o‘chirish', 'Add to review': 'Tekshirishga qo‘shish',
      'Camera role': 'Kamera roli', 'No role': 'Rolsiz',
      'Entrance · entered': 'Kirish · kirdi', 'Exit · left': 'Chiqish · chiqdi',
      'Crossing direction': 'O‘tish yo‘nalishi',
      'Save changes': 'O‘zgarishlarni saqlash', 'Edit camera': 'Kamerani tahrirlash',
      'Edit person': 'Shaxsni tahrirlash', 'Enroll': 'Ro‘yxatga olish',
      'Add samples': 'Namunalar qo‘shish', 'Photos / videos': 'Rasmlar / videolar',
      'Username': 'Foydalanuvchi nomi', 'Sign in': 'Kirish', 'Signing in…': 'Kirilmoqda…',
      'Sign in to the monitoring dashboard': 'Kuzatuv paneliga kirish',
      'Enter your username and password.': 'Foydalanuvchi nomi va parolni kiriting.',
      'Sign-in failed. Try again.': 'Kirish amalga oshmadi. Qayta urinib ko‘ring.',
      'Invalid username or password.': 'Foydalanuvchi nomi yoki parol noto‘g‘ri.',
      'This account is disabled.': 'Bu hisob o‘chirilgan.',
      'This account is locked after too many failed sign-in attempts. Ask an administrator to unlock it.': 'Ko‘p marta noto‘g‘ri kirish tufayli hisob bloklangan. Administratorga murojaat qiling.',
      'This account has been locked after too many failed sign-in attempts. Ask an administrator to unlock it.': 'Ko‘p marta noto‘g‘ri kirish tufayli hisob bloklangan. Administratorga murojaat qiling.',
      'Could not reach the server. Check your connection.': 'Serverga ulanib bo‘lmadi. Ulanishni tekshiring.',
      'Change password': 'Parolni o‘zgartirish', 'Current password': 'Joriy parol',
      'New password': 'Yangi parol', 'Confirm new password': 'Yangi parolni tasdiqlang',
      'Update password': 'Parolni yangilash', 'Account enabled': 'Hisob faol',
      '+ Add account': '+ Hisob qo‘shish', 'Save account': 'Hisobni saqlash',
      'Add account': 'Hisob qo‘shish', 'Edit account': 'Hisobni tahrirlash',
      'Unlock': 'Qulfdan chiqarish', 'Disable': 'O‘chirish', 'Enable': 'Yoqish',
      'Server capacity': 'Server quvvati', 'Concurrent cameras': 'Bir vaqtdagi kameralar',
      'Camera FPS': 'Kamera kadr tezligi', 'Stream width (pixels)': 'Oqim kengligi (piksel)',
      'Stream height (pixels)': 'Oqim balandligi (piksel)',
      'People per frame at peak': 'Eng ko‘p odamlar soni (bir kadrda)',
      'Simultaneous live viewers': 'Bir vaqtdagi tomoshabinlar',
      'Stream bitrate (Mbps/camera)': 'Oqim tezligi (Mbit/s, har kamera)',
      'Estimated snapshots (MB/day total)': 'Suratlar hajmi (MB/kun)',
      'Retention (days)': 'Saqlash muddati (kun)',
      'Check this server (15 seconds)': 'Serverni tekshirish (15 soniya)',
      'What remains unverified': 'Hali tekshirilmagan jihatlar', 'Bottlenecks': 'Cheklovlar',
      'This server': 'Ushbu server', 'Observed cameras': 'Kuzatilgan kameralar',
      'Not yet verified': 'Hali tekshirilmagan',
      'Delete event': 'Hodisani o‘chirish', 'Delete records': 'Yozuvlarni o‘chirish',
      'All customer events': 'Barcha mijoz hodisalari',
      'All cameras': 'Barcha kameralar', 'All labels': 'Barcha belgilar',
      'Upload': 'Yuklash', 'Disconnect': 'Uzish',
      'Verify connection': 'Ulanishni tekshirish', 'Save settings': 'Sozlamalarni saqlash',
      'Not configured': 'Sozlanmagan', 'Configured': 'Sozlangan',
      'Connection error': 'Ulanish xatosi',
      'Source type': 'Manba turi', 'Default device': 'Standart qurilma',
      'Default FPS': 'Standart kadr tezligi',
      'RTSP URL': 'RTSP URL', 'FPS limit': 'FPS chegarasi',
      'Activity log': 'Faoliyat jurnali', 'Check feeds with these credentials': 'Ushbu ma’lumotlar bilan oqimni tekshirish',
      'Run CV Model': 'Modelni ishga tushirish', 'Stop CV Model': 'Modelni to‘xtatish',
      'Advanced scan options': 'Qo‘shimcha qidiruv sozlamalari',
      'Discover NVR & Camera': 'Registrator va kamerani topish',
      'Dashboard language': 'Panel tili',
      'Open menu': 'Menyuni ochish', 'Close menu': 'Menyuni yopish',
      'Main navigation': 'Asosiy navigatsiya',
    },
  };

  Object.assign(translations.ru, {
    'Attendance, customers, recent events and alerts at a glance.': 'Посещаемость, посетители и последние оповещения.',
    'Add cameras, tune settings, and watch live analytics.': 'Добавляйте камеры, настраивайте параметры и смотрите прямую трансляцию.',
    'Register the people the system should recognise.': 'Зарегистрируйте людей для распознавания.',
    'Entrance and exit records confirmed by Telegram administrators.': 'Записи входа и выхода, подтверждённые администраторами Telegram.',
    'Customer-tagged entrance and exit events, including unconfirmed ones.': 'Входы и выходы посетителей, включая неподтверждённые.',
    'Measure this server against a proposed camera workload.': 'Проверьте сервер под запланированную нагрузку камер.',
    'Review phone-use examples and collect missed cases.': 'Проверяйте примеры использования телефона и добавляйте пропущенные случаи.',
    'Bot settings and admin-confirmed phone-use events.': 'Настройки бота и события использования телефона.',
    'Administrators, readers, passwords and sign-in protection.': 'Администраторы, просмотр, пароли и защита входа.',
    'Confirmed entrance and exit crossings for enrolled people.': 'Подтверждённые проходы зарегистрированных людей.',
    'Phone-use and entrance/exit incidents, including events that are not confirmed yet.': 'Использование телефона, входы и выходы, включая неподтверждённые события.',
    'Events waiting for a Telegram group administrator to confirm or reject them.': 'События, ожидающие решения администратора группы Telegram.',
    'The latest confirmed entrance/exit records assigned to enrolled people.': 'Последние подтверждённые входы и выходы зарегистрированных людей.',
    'Confirmed entrance/exit events that were not linked to an enrolled person. They are kept for review and do not affect attendance.': 'Подтверждённые проходы без назначенного человека. Они сохраняются для проверки и не влияют на посещаемость.',
    'Built from entrance and exit crossings confirmed by a Telegram group administrator. Only enrolled people appear here; unknown crossings are saved below but never change attendance.': 'Основано на входах и выходах, подтверждённых администратором Telegram. Здесь показаны только зарегистрированные люди; неизвестные проходы не меняют посещаемость.',
    'Phone-use and entrance/exit incidents. Unconfirmed events are marked': 'Использование телефона, входы и выходы. Неподтверждённые события отмечены как',
    'Entrance/exit crossings from cameras marked': 'Входы и выходы с камер, помеченных как',
    'Unconfirmed events are shown too, labelled': 'Неподтверждённые события также показаны как',
    'Account enabled': 'Учётная запись активна',
    'Accounts lock automatically after three consecutive wrong passwords and must be unlocked by an administrator.': 'После трёх неверных паролей учётная запись блокируется; разблокировать её может администратор.',
    'Access is logged. Accounts lock automatically after three consecutive wrong passwords and must be unlocked by an administrator.': 'Входы записываются. После трёх неверных паролей учётная запись блокируется; разблокировать её может администратор.',
    'You are signed in with a read-only account. Configuration, accounts and other changes are disabled.': 'Вы вошли в режиме просмотра. Изменение настроек и учётных записей недоступно.',
    'Check this server (15 seconds)': 'Проверить сервер (15 секунд)',
    'Assess a proposed workload against this server. The 15-second check observes running cameras; it does not start streams or add test load.': 'Оцените запланированную нагрузку. Проверка наблюдает за работающими камерами 15 секунд, не запуская новые потоки.',
    'Automatic snapshot capture': 'Автоматическое сохранение снимков',
    'Maximum snapshots per camera per rolling hour': 'Максимум снимков с камеры за последний час',
    'Maximum new images per camera per rolling hour': 'Максимум новых изображений с камеры за последний час',
    'No samples yet.': 'Образцов пока нет.',
    'No unrecognised face candidates yet.': 'Неизвестных лиц для проверки пока нет.',
    'No active streams. Select a camera and click Run CV Model.': 'Активных потоков нет. Выберите камеру и запустите модель.',
    'Select a camera from the list to configure and run it.': 'Выберите камеру для настройки и запуска.',
    'No customer events match this status.': 'Нет событий посетителей с таким статусом.',
    'No accounts found.': 'Учётные записи не найдены.',
    'Loading accounts…': 'Загрузка учётных записей…',
    'Counting customers': 'Подсчёт посетителей',
    'Sign-in protection': 'Защита входа', 'Bot token': 'Токен бота',
    'Group ID': 'ID группы', 'Image or short clip': 'Изображение или короткое видео',
    'Extra subnets or hosts (optional)': 'Дополнительные сети или адреса (необязательно)',
    'Device username': 'Имя пользователя устройства', 'Device password': 'Пароль устройства',
    'Device (compute)': 'Устройство для вычислений', 'ONVIF username': 'Имя пользователя ONVIF',
    'ONVIF password': 'Пароль ONVIF', 'Add URL': 'Добавить URL',
    'Custom maximum (1–120)': 'Другой максимум (1–120)',
    'Custom maximum (1–30)': 'Другой максимум (1–30)',
    'Assign a role': 'Назначить роль', 'Phone sensitivity': 'Чувствительность к телефону',
    'Look-down threshold': 'Порог наклона головы',
    'Load camera frame to draw line': 'Загрузить кадр для разметки линии',
    'Clear line': 'Очистить линию', 'Enroll person': 'Зарегистрировать человека',
    'Notes': 'Заметки', '(optional)': '(необязательно)',
    'Are you sure?': 'Вы уверены?',
    'Draw person box': 'Выделить человека', 'Save box': 'Сохранить область',
    'Clear box': 'Очистить область', 'Add account': 'Добавить учётную запись',
    'Only administrators can see this section.': 'Раздел доступен только администраторам.',
  });

  Object.assign(translations.uz, {
    'Attendance, customers, recent events and alerts at a glance.': 'Davomat, mijozlar va so‘nggi ogohlantirishlar bir joyda.',
    'Add cameras, tune settings, and watch live analytics.': 'Kameralarni qo‘shing, sozlang va jonli videoni kuzating.',
    'Register the people the system should recognise.': 'Tizim tanishi kerak bo‘lgan shaxslarni ro‘yxatga oling.',
    'Entrance and exit records confirmed by Telegram administrators.': 'Telegram administratorlari tasdiqlagan kirish va chiqishlar.',
    'Customer-tagged entrance and exit events, including unconfirmed ones.': 'Mijoz sifatida belgilangan kirish va chiqishlar, tasdiqlanmaganlari bilan.',
    'Measure this server against a proposed camera workload.': 'Serverni rejalashtirilgan kamera yuklamasi uchun tekshiring.',
    'Review phone-use examples and collect missed cases.': 'Telefon ishlatilishi misollarini tekshiring va o‘tkazib yuborilgan holatlarni qo‘shing.',
    'Bot settings and admin-confirmed phone-use events.': 'Bot sozlamalari va telefon hodisalari.',
    'Administrators, readers, passwords and sign-in protection.': 'Administratorlar, ko‘ruvchilar, parollar va kirish himoyasi.',
    'Confirmed entrance and exit crossings for enrolled people.': 'Ro‘yxatdagi shaxslarning tasdiqlangan kirish va chiqishlari.',
    'Phone-use and entrance/exit incidents, including events that are not confirmed yet.': 'Telefon, kirish va chiqish hodisalari, tasdiqlanmaganlari bilan.',
    'Events waiting for a Telegram group administrator to confirm or reject them.': 'Telegram guruhi administratori tasdiqlashini yoki rad etishini kutayotgan hodisalar.',
    'The latest confirmed entrance/exit records assigned to enrolled people.': 'Ro‘yxatdagi shaxslarga biriktirilgan so‘nggi tasdiqlangan kirish va chiqishlar.',
    'Confirmed entrance/exit events that were not linked to an enrolled person. They are kept for review and do not affect attendance.': 'Ro‘yxatdagi shaxsga biriktirilmagan tasdiqlangan o‘tishlar. Ular tekshirish uchun saqlanadi, ammo davomatga ta’sir qilmaydi.',
    'Built from entrance and exit crossings confirmed by a Telegram group administrator. Only enrolled people appear here; unknown crossings are saved below but never change attendance.': 'Telegram administratori tasdiqlagan kirish va chiqishlarga asoslanadi. Faqat ro‘yxatdagi shaxslar ko‘rsatiladi; noma’lum o‘tishlar davomatni o‘zgartirmaydi.',
    'Phone-use and entrance/exit incidents. Unconfirmed events are marked': 'Telefon, kirish va chiqish hodisalari. Tasdiqlanmaganlar quyidagicha belgilanadi:',
    'Entrance/exit crossings from cameras marked': 'Mijoz deb belgilangan kameralardagi kirish va chiqishlar:',
    'Unconfirmed events are shown too, labelled': 'Tasdiqlanmagan hodisalar ham quyidagicha ko‘rsatiladi:',
    'Accounts lock automatically after three consecutive wrong passwords and must be unlocked by an administrator.': 'Uch marta noto‘g‘ri parol kiritilsa hisob bloklanadi; uni administrator ochadi.',
    'Access is logged. Accounts lock automatically after three consecutive wrong passwords and must be unlocked by an administrator.': 'Kirishlar qayd etiladi. Uch marta noto‘g‘ri parol kiritilsa hisob bloklanadi; uni administrator ochadi.',
    'You are signed in with a read-only account. Configuration, accounts and other changes are disabled.': 'Siz faqat ko‘rish huquqi bilan kirdingiz. Sozlamalar va hisoblarni o‘zgartirib bo‘lmaydi.',
    'Assess a proposed workload against this server. The 15-second check observes running cameras; it does not start streams or add test load.': 'Rejalashtirilgan yuklamani baholang. Tekshiruv 15 soniya ishlayotgan kameralarni kuzatadi, yangi oqimlarni ishga tushirmaydi.',
    'Maximum snapshots per camera per rolling hour': 'Har kamera uchun bir soatdagi maksimal suratlar',
    'Maximum new images per camera per rolling hour': 'Har kamera uchun bir soatdagi yangi rasmlar soni',
    'No samples yet.': 'Hozircha namunalar yo‘q.',
    'No unrecognised face candidates yet.': 'Tekshirish uchun noma’lum yuzlar yo‘q.',
    'No active streams. Select a camera and click Run CV Model.': 'Faol oqim yo‘q. Kamerani tanlang va modelni ishga tushiring.',
    'Select a camera from the list to configure and run it.': 'Sozlash va ishga tushirish uchun kamerani tanlang.',
    'No customer events match this status.': 'Bu holatga mos mijoz hodisalari yo‘q.',
    'No accounts found.': 'Hisoblar topilmadi.',
    'Loading accounts…': 'Hisoblar yuklanmoqda…',
    'Counting customers': 'Mijozlarni sanash',
    'Sign-in protection': 'Kirish himoyasi', 'Bot token': 'Bot tokeni',
    'Group ID': 'Guruh IDsi', 'Image or short clip': 'Rasm yoki qisqa video',
    'Extra subnets or hosts (optional)': 'Qo‘shimcha tarmoqlar yoki manzillar (ixtiyoriy)',
    'Device username': 'Qurilma foydalanuvchi nomi', 'Device password': 'Qurilma paroli',
    'Device (compute)': 'Hisoblash qurilmasi', 'ONVIF username': 'ONVIF foydalanuvchi nomi',
    'ONVIF password': 'ONVIF paroli', 'Add URL': 'URL qo‘shish',
    'Custom maximum (1–120)': 'Boshqa cheklov (1–120)',
    'Custom maximum (1–30)': 'Boshqa cheklov (1–30)',
    'Assign a role': 'Rol tayinlash', 'Phone sensitivity': 'Telefon sezgirligi',
    'Look-down threshold': 'Bosh egilishi chegarasi',
    'Load camera frame to draw line': 'Chiziq chizish uchun kadrni yuklash',
    'Clear line': 'Chiziqni tozalash', 'Enroll person': 'Shaxsni ro‘yxatga olish',
    'Notes': 'Izohlar', '(optional)': '(ixtiyoriy)',
    'Are you sure?': 'Ishonchingiz komilmi?',
    'Draw person box': 'Shaxsni belgilash', 'Save box': 'Hududni saqlash',
    'Clear box': 'Hududni tozalash',
    'Only administrators can see this section.': 'Bu bo‘lim faqat administratorlar uchun.',
  });

  const locales = { en: 'en-US', ru: 'ru-RU', uz: 'uz-UZ' };
  const localeKey = 'cctv-language';
  let language;
  try { language = localStorage.getItem(localeKey) || 'en'; } catch { language = 'en'; }
  if (!(language in locales)) language = 'en';
  const originals = new WeakMap();
  const attributes = new WeakMap();
  const translatableAttributes = ['placeholder', 'title', 'aria-label'];

  function translateText(node) {
    const value = node.nodeValue;
    const record = originals.get(node);
    const original = record && value === record.rendered ? record.original : value;
    const trimmed = original.trim();
    const key = trimmed.replace(/\s+/g, ' ');
    const translated = translations[language]?.[key];
    const rendered = translated
      ? `${original.slice(0, original.indexOf(trimmed))}${translated}${original.slice(original.indexOf(trimmed) + trimmed.length)}`
      : original;
    originals.set(node, { original, rendered });
    if (value !== rendered) node.nodeValue = rendered;
  }

  function translateElement(el) {
    const record = attributes.get(el) || {};
    for (const attr of translatableAttributes) {
      if (!el.hasAttribute(attr)) continue;
      const value = el.getAttribute(attr);
      const original = record[attr] && value === record[attr].rendered
        ? record[attr].original : value;
      const rendered = translations[language]?.[original] || original;
      record[attr] = { original, rendered };
      if (value !== rendered) el.setAttribute(attr, rendered);
    }
    attributes.set(el, record);
  }

  function translateTree(root) {
    if (root.nodeType === Node.TEXT_NODE) { translateText(root); return; }
    if (root.nodeType !== Node.ELEMENT_NODE) return;
    if (root.closest('[data-no-translate], script, style, textarea, code, pre')) return;
    translateElement(root);
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
      const node = walker.currentNode;
      if (node.nodeType === Node.ELEMENT_NODE) {
        if (!node.closest('[data-no-translate], script, style, textarea, code, pre')) translateElement(node);
      } else if (!node.parentElement?.closest('[data-no-translate], script, style, textarea, code, pre')) {
        translateText(node);
      }
    }
  }

  function setLanguage(next) {
    if (!(next in locales)) return;
    language = next;
    try { localStorage.setItem(localeKey, next); } catch { /* private mode */ }
    document.documentElement.lang = next;
    document.querySelectorAll('[data-language-switch]').forEach((select) => { select.value = next; });
    translateTree(document.body);
    window.dispatchEvent(new Event('dashboard-language-change'));
  }

  window.dashboardI18n = {
    get language() { return language; },
    get locale() { return locales[language]; },
    t(key) { return translations[language]?.[key] || key; },
    setLanguage,
  };
  document.documentElement.lang = language;
  translateTree(document.body);
  document.querySelectorAll('[data-language-switch]').forEach((select) => { select.value = language; });
  document.addEventListener('change', (event) => {
    if (event.target.matches('[data-language-switch]')) setLanguage(event.target.value);
  });
  new MutationObserver((changes) => {
    for (const change of changes) {
      if (change.type === 'characterData') {
        if (!change.target.parentElement?.closest('[data-no-translate], script, style, textarea, code, pre')) {
          translateText(change.target);
        }
      }
      else if (change.type === 'attributes') translateElement(change.target);
      else for (const node of change.addedNodes) translateTree(node);
    }
  }).observe(document.body, {
    childList: true, subtree: true, characterData: true,
    attributes: true, attributeFilter: translatableAttributes,
  });
})();
