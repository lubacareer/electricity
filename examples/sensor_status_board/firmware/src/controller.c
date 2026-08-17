#include "controller.h"

#include <math.h>

#define ADC_FULL_SCALE 4095.0f
#define ADC_FAULT_LOW 8U
#define ADC_FAULT_HIGH 4087U
#define R_FIXED_OHM 10000.0f
#define R_NOMINAL_OHM 10000.0f
#define BETA_K 3950.0f
#define T_NOMINAL_K 298.15f
#define ALARM_ON_C 35.0f
#define ALARM_OFF_C 33.0f

static controller_state_t state = CONTROLLER_NORMAL;
static bool acknowledged = false;

static float adc_to_temperature_c(uint16_t adc_raw) {
    const float ratio = (float)adc_raw / ADC_FULL_SCALE;
    const float resistance = R_FIXED_OHM * ratio / (1.0f - ratio);
    const float inverse_kelvin = (1.0f / T_NOMINAL_K) +
                                 (logf(resistance / R_NOMINAL_OHM) / BETA_K);
    return (1.0f / inverse_kelvin) - 273.15f;
}

void controller_reset(void) {
    state = CONTROLLER_NORMAL;
    acknowledged = false;
}

controller_outputs_t controller_step(uint16_t adc_raw, bool acknowledge_pressed) {
    controller_outputs_t output = {0};

    if (adc_raw <= ADC_FAULT_LOW || adc_raw >= ADC_FAULT_HIGH) {
        state = CONTROLLER_SENSOR_FAULT;
        acknowledged = false;
        output.temperature_c = NAN;
    } else {
        const float temperature_c = adc_to_temperature_c(adc_raw);
        output.temperature_c = temperature_c;

        if (state == CONTROLLER_SENSOR_FAULT) {
            state = temperature_c >= ALARM_ON_C ? CONTROLLER_ALARM : CONTROLLER_NORMAL;
        } else if (state == CONTROLLER_NORMAL && temperature_c >= ALARM_ON_C) {
            state = CONTROLLER_ALARM;
            acknowledged = false;
        } else if (state == CONTROLLER_ALARM && temperature_c < ALARM_OFF_C) {
            state = CONTROLLER_NORMAL;
            acknowledged = false;
        }

        if (state == CONTROLLER_ALARM && acknowledge_pressed) {
            acknowledged = true;
        }
    }

    output.state = state;
    output.acknowledged = acknowledged;
    output.green_led = state == CONTROLLER_NORMAL;
    output.red_led = state != CONTROLLER_NORMAL;
    output.buzzer = state == CONTROLLER_SENSOR_FAULT ||
                    (state == CONTROLLER_ALARM && !acknowledged);
    return output;
}
