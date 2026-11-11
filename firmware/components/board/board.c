#include "board.h"

#include "driver/gpio.h"
#include "esp_flash.h"
#include "esp_log.h"
#include "esp_system.h"
#include "soc/rtc_cntl_reg.h"
#include "soc/usb_serial_jtag_reg.h"

static const char *TAG = "board";

esp_err_t board_init(void)
{
    const gpio_config_t btn = {
        .pin_bit_mask = 1ULL << BOARD_BUTTON_PIN,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
    };
    ESP_ERROR_CHECK(gpio_config(&btn));

    const gpio_config_t led = {
        .pin_bit_mask = (1ULL << BOARD_LED_PIN_DATA) | (1ULL << BOARD_LED_PIN_CLK),
        .mode = GPIO_MODE_OUTPUT,
    };
    ESP_ERROR_CHECK(gpio_config(&led));
    gpio_set_level(BOARD_LED_PIN_DATA, 0);
    gpio_set_level(BOARD_LED_PIN_CLK, 0);
    board_led_set(0, 0, 0, 0);

    ESP_LOGI(TAG, "board init done, flash size %lu bytes", (unsigned long)board_flash_size());
    return ESP_OK;
}

bool board_button_pressed(void)
{
    return gpio_get_level(BOARD_BUTTON_PIN) == 0;
}

static void apa102_byte(uint8_t v)
{
    for (int i = 7; i >= 0; i--) {
        gpio_set_level(BOARD_LED_PIN_DATA, (v >> i) & 1);
        gpio_set_level(BOARD_LED_PIN_CLK, 1);
        gpio_set_level(BOARD_LED_PIN_CLK, 0);
    }
}

void board_led_set(uint8_t r, uint8_t g, uint8_t b, uint8_t brightness)
{
    // APA102 frame: 32 bit start frame, LED frame (0b111 + 5 bit brightness, B, G, R), end frame.
    for (int i = 0; i < 4; i++) {
        apa102_byte(0x00);
    }
    apa102_byte(0xE0 | (brightness & 0x1F));
    apa102_byte(b);
    apa102_byte(g);
    apa102_byte(r);
    for (int i = 0; i < 4; i++) {
        apa102_byte(0xFF);
    }
}

uint32_t board_flash_size(void)
{
    uint32_t size = 0;
    if (esp_flash_get_physical_size(esp_flash_default_chip, &size) != ESP_OK) {
        return 0;
    }
    return size;
}

void board_restart_to_download_mode(void)
{
    ESP_LOGW(TAG, "restarting into ROM download mode");
    // TinyUSB routes the internal USB PHY to the OTG controller via RTC_CNTL_USB_CONF.
    // That RTC register survives a software reset, so the ROM's USB-Serial/JTAG would
    // stay disconnected (verified on the device: nothing enumerated). Give the PHY back
    // to USB-Serial/JTAG first (same as the Arduino core's usb_switch_to_cdc_jtag()).
    CLEAR_PERI_REG_MASK(RTC_CNTL_USB_CONF_REG, RTC_CNTL_SW_HW_USB_PHY_SEL | RTC_CNTL_SW_USB_PHY_SEL);
    CLEAR_PERI_REG_MASK(USB_SERIAL_JTAG_CONF0_REG, USB_SERIAL_JTAG_PHY_SEL);
    SET_PERI_REG_MASK(USB_SERIAL_JTAG_CONF0_REG, USB_SERIAL_JTAG_USB_PAD_ENABLE | USB_SERIAL_JTAG_DP_PULLUP);
    REG_WRITE(RTC_CNTL_OPTION1_REG, RTC_CNTL_FORCE_DOWNLOAD_BOOT);
    esp_restart();
}
