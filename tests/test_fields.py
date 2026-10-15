from dataclasses import dataclass

from playerok_bot.fields import DeliveryFieldMapper, FieldOverrides
from playerok_bot.models import Credential


@dataclass
class FakeField:
    label: str
    value: str | None = None


def test_updates_separate_delivery_fields() -> None:
    fields = [FakeField("Логин"), FakeField("Пароль"), FakeField("Комментарий")]
    mapper = DeliveryFieldMapper(FieldOverrides(), "приятного пользования")
    credential = Credential("user@example.test", "secret")

    mapper.apply(fields, credential)

    assert [field.value for field in fields] == [
        "user@example.test",
        "secret",
        "приятного пользования",
    ]
    assert mapper.extract(fields) == credential


def test_updates_combined_delivery_field() -> None:
    fields = [FakeField("Данные аккаунта")]
    mapper = DeliveryFieldMapper(FieldOverrides(), "приятного пользования")
    credential = Credential("user@example.test", "secret")

    mapper.apply(fields, credential)

    assert mapper.extract(fields) == credential
    assert "Комментарий: приятного пользования" in fields[0].value

