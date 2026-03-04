# CANopenNode STM32

CANopenSTM32 is a CANopen stack running on STM32 microcontroller based on [CANOpenNode](https://github.com/CANopenNode/CANopenNode) stack.

## How to run demos

Examples are developed in [STM32CubeIDE](https://www.st.com/en/development-tools/stm32cubeide.html) tool,
official ST development studio for any STM32 microcontroller.
You can directly open projects in the STM32CubeIDE and run examples on the relevant boards.

## Repository directories

- `./CANopenNode` : Includes the stack implementation, for most of use cases you don't need to touch these files as they are constant between all the variations and ports (i.e. Linux, PIC, STM32 and etc.)
- `./CANopenNode_drivers_STM32` : Includes the implementation of low-level driver for STM32 microcontrollers, supports CAN-based controllers and FDCAN without major changes.
- `./projects/stm32f429i-nucleo_can` : Active example project for STM32F429I Nucleo board.
- `./Legacy` : Includes an older version of CANOpenSTM32 implementation, specifically made for FDCAN controllers.

## Supported boards and MCUs

### STM32F429I-Nucleo (current target)

The current maintained project in this repository targets STM32F429I Nucleo with classic bxCAN.

- Runs on `projects/stm32f429i-nucleo_can`
- Uses bxCAN peripheral
- Uses `TIM14` as 1 ms CANopen real-time tick
- Main loop calls `canopen_app_process()`
- Timer interrupt calls `canopen_app_interrupt()`

## Video Tutorial

To get a good grasp of CANOpenNode Stack and CANOpenNodeSTM32 stack, you can refer to this video, which explains from basics to implementation and porting of the CANOpenNode stack.

[![CANOpen Node STM32 From basics to coding](https://img.youtube.com/vi/R-r5qIOTjOo/0.jpg)](https://www.youtube.com/watch?v=R-r5qIOTjOo)

>[00:00](https://www.youtube.com/watch?v=R-r5qIOTjOo&t=0s) Introduction and Overview [1:13](https://www.youtube.com/watch?v=R-r5qIOTjOo&t=73s) Why CAN ? [4:51](https://www.youtube.com/watch?v=R-r5qIOTjOo&t=291s) CAN Bus [8:55](https://www.youtube.com/watch?v=R-r5qIOTjOo&t=535s) Why CANOpen ? [13:27](https://www.youtube.com/watch?v=R-r5qIOTjOo&t=807s) CANOpen architecture [20:00](https://www.youtube.com/watch?v=R-r5qIOTjOo&t=1200s) Object dictionary [21:38](https://www.youtube.com/watch?v=R-r5qIOTjOo&t=1298s) Important CANOpen concepts [23:29](https://www.youtube.com/watch?v=R-r5qIOTjOo&t=1409s) PDO [27:25](https://www.youtube.com/watch?v=R-r5qIOTjOo&t=1645s) SDO [32:23](https://www.youtube.com/watch?v=R-r5qIOTjOo&t=1943s) NMT [33:25](https://www.youtube.com/watch?v=R-r5qIOTjOo&t=2005s) CANOpenNode Open-Source Stack [39:26](https://www.youtube.com/watch?v=R-r5qIOTjOo&t=2366s) STM32 Practical implementation [40:29](https://www.youtube.com/watch?v=R-r5qIOTjOo&t=2429s) CANOpen Tutorial code preparation [43:09](https://www.youtube.com/watch?v=R-r5qIOTjOo&t=2589s) Importing examples to STM32CubeIDE and programming them [47:04](https://www.youtube.com/watch?v=R-r5qIOTjOo&t=2824s) Examples explanation [57:00](https://www.youtube.com/watch?v=R-r5qIOTjOo&t=3420s) Porting to custom STM32 board [1:18:20](https://www.youtube.com/watch?v=R-r5qIOTjOo&t=4700s) EDS Editor (Object dictionary editor) [1:25:54](https://www.youtube.com/watch?v=R-r5qIOTjOo&t=5154s) Creating a TPDO [1:39:55](https://www.youtube.com/watch?v=R-r5qIOTjOo&t=5995s) Accessing OD Variables [1:54:08](https://www.youtube.com/watch?v=R-r5qIOTjOo&t=6848s) Creating an RPDO [2:05:50](https://www.youtube.com/watch?v=R-r5qIOTjOo&t=7550s) Using the SDOs [2:52:52](https://www.youtube.com/watch?v=R-r5qIOTjOo&t=10372s) Node guarding [3:04:38](https://www.youtube.com/watch?v=R-r5qIOTjOo&t=11078s) Transmitting PDOs manually

## Porting to other STM32 microcontrollers checklist :
- Create a new project in STM32CubeIDE
- Configure CAN/FDCAN to your desired bitrate and map it to relevant tx/rx pins - Make sure you activate Auto Bus recovery (bxCAN) / protocol exception handling (FDCAN)
- Activate the RX and TX interrupt on the CAN peripheral
- Enable a timer for a 1 ms overflow interrupt and activate interrupt for that timer
- Copy or clone `CANopenNode` and `CANopenNode_drivers_STM32` into your project directory
- Add `CANopenNode` and `CANopenNode_drivers_STM32` to Source locations in `Project Properties -> C/C++ General -> Paths and Symbols -> Source Locations`
  - add an exclusion filter for `example/` folder for `CANopenNode` folder
- Add `CANopenNode` and `CANopenNode_drivers_STM32` to `Project Properties -> C/C++ General -> Paths and Symbols -> Includes` under `GNU C` items
- In your main.c, add `#include "CO_app_STM32.h"`
  ```c
    /* Private includes ----------------------------------------------------------*/
    /* USER CODE BEGIN Includes */
    #include "CO_app_STM32.h"
    /* USER CODE END Includes */
  ```
  - Make sure that you have the `HAL_TIM_PeriodElapsedCallback` function implemented with a call to `canopen_app_interrupt`.

```c
void HAL_TIM_PeriodElapsedCallback(TIM_HandleTypeDef *htim)
{
  /* USER CODE BEGIN Callback 0 */

  /* USER CODE END Callback 0 */
  if (htim->Instance == TIM1) {
    HAL_IncTick();
  }
  /* USER CODE BEGIN Callback 1 */
  // Handle CANOpen app interrupts
  if (htim == canopenNodeSTM32->timerHandle) {
      canopen_app_interrupt();
  }
  /* USER CODE END Callback 1 */
}

```

- Now based on your application, you'll take one of the following approaches :

### In Bare-metal application
- In your main.c, add following code to your USER CODE BEGIN 2
  ```c
    /* USER CODE BEGIN 2 */
    CANopenNodeSTM32 canOpenNodeSTM32;
    canOpenNodeSTM32.CANHandle = &hcan1;
    canOpenNodeSTM32.HWInitFunction = MX_CAN1_Init;
    canOpenNodeSTM32.timerHandle = &htim14;
    canOpenNodeSTM32.desiredNodeID = 1;
    canOpenNodeSTM32.baudrate = 125;
    canopen_app_init(&canOpenNodeSTM32);
    /* USER CODE END 2 */
  ```

- In your main.c, add following code to your USER CODE BEGIN WHILE
  ```c
  /* USER CODE BEGIN WHILE */
  while (1)
  {
      canopen_app_process();
    /* USER CODE END WHILE */

  ```

### In FreeRTOS Applications
- You can create a high priority CANopen task and call `canopen_app_process()` cyclically in that task.

> In RTOS applications, be very careful when accessing OD variables, CAN Send and EMCY variable. You should lock these critical sections to prevent race conditions. Have a look at `CO_LOCK_CAN_SEND`, `CO_LOCK_OD` and `CO_LOCK_EMCY`.

- Run your project on the target board, you should be able to see bootup message on startup

### Known limitations :

- We have never tested the multiple CANOpen on a single STM32 device, but the original CANOpenNode has the capability to use multi modules, which you can develop yourself.

### Clone or update

Clone the project from git repository and get submodules:

```bash
git clone https://github.com/CANopenNode/CANopenSTM32
cd CANopenSTM32
git submodule update --init --recursive
```

Update an existing project including submodules:

```bash
cd CANopenSTM32
git pull
git submodule update --init --recursive
```

## License

This file is part of CANopenNode, an open-source CANopen Stack. Project home page is https://github.com/CANopenNode/CANopenNode. For more information on CANopen see http://www.can-cia.org/.

Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance with the License. You may obtain a copy of the License at

http://www.apache.org/licenses/LICENSE-2.0
