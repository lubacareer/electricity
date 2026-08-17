#ifndef SMD_TWIN_CONTROLLER_H
#define SMD_TWIN_CONTROLLER_H

#include <stdbool.h>
#include <stdint.h>

typedef enum {
    CONTROLLER_NORMAL = 0,
    CONTROLLER_ALARM = 1,
    CONTROLLER_SENSOR_FAULT = 2
} controller_state_t;

typedef struct {
    controller_state_t state;
    bool green_led;
    bool red_led;
    bool buzzer;
    bool acknowledged;
    float temperature_c;
} controller_outputs_t;

void controller_reset(void);
controller_outputs_t controller_step(uint16_t adc_raw, bool acknowledge_pressed);

#endif
