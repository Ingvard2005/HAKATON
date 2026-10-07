"""Phone syntax and length checks; no network or claims that a number exists."""
import re
import phonenumbers


def normalize_phone(value, region="BY"):
    value = (value or "").strip()
    if not value:
        return ""
    if not re.fullmatch(r"[0-9+()\s.\-]+", value) or value.count("+") > 1 or ("+" in value and not value.startswith("+")):
        raise ValueError("Телефон может содержать цифры, пробелы, скобки и дефисы. Знак + допускается только в начале.")
    if len(re.sub(r"[^0-9]", "", value)) > (17 if value.startswith("00") else 15):
        raise ValueError("Проверьте длину номера. Международный номер содержит не более 15 цифр; для Беларуси: +375 и 9 цифр.")
    try:
        number = phonenumbers.parse("+" + value[2:] if value.startswith("00") else value, region)
    except phonenumbers.NumberParseException:
        raise ValueError("Не удалось распознать телефон. Укажите код страны, например +375 29 123-45-67.") from None
    if not phonenumbers.is_possible_number(number):
        raise ValueError("Проверьте длину номера. Для Беларуси: +375 и 9 цифр, например +375 29 123-45-67.")
    return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)


def display_phone(value):
    if not value:
        return ""
    try:
        normalized = normalize_phone(value)
        return phonenumbers.format_number(phonenumbers.parse(normalized, None), phonenumbers.PhoneNumberFormat.INTERNATIONAL)
    except ValueError:
        # Existing data is not rewritten or silently truncated by presentation.
        return str(value)


def normalize_masked_phone(value):
    if not value:
        return ""
    if len(re.sub(r"[^0-9]", "", value)) != 12:
        raise ValueError("Введите полный номер: +XXX XX XXX-XX-XX.")
    normalized = normalize_phone(value)
    if not phonenumbers.is_valid_number(phonenumbers.parse(normalized, None)):
        raise ValueError("Проверьте код страны и код оператора: номер не соответствует телефонному плану.")
    return normalized
