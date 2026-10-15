class BotError(Exception):
    """Безопасная для вывода пользователю ошибка приложения."""


class ConfigurationError(BotError):
    """Ошибка конфигурации."""


class AccountsFormatError(BotError):
    """Некорректная строка в файле аккаунтов."""


class FieldMappingError(BotError):
    """Не удалось сопоставить поля данных товара."""


class ListingValidationError(BotError):
    """Лот не подходит для безопасного запуска бота."""


class PremiumPriceLimitError(BotError):
    """Цена повторной премиум-публикации превышает заданный лимит."""


class InventoryNotReadyError(BotError):
    """Для первого запуска ещё не загружен аккаунт текущего лота."""

    def __init__(self, message: str, lot_numbers=()):
        super().__init__(message)
        self.lot_numbers = tuple(lot_numbers)
