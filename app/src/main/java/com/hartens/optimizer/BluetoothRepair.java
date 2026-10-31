package com.hartens.optimizer;

import java.util.regex.Pattern;

/** Read-only Bluetooth status and safe access to TV pairing settings.
 *  Never restart Bluetooth, delete bonds, or touch remote-control services.
 */
final class BluetoothRepair {
 private final AdbClient adb;
 BluetoothRepair(AdbClient a){adb=a;}
 private static final Pattern MAC=Pattern.compile("(?i)\\b(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\\b");
 private static String clean(String s){return MAC.matcher(s).replaceAll("**:**:**:**:**:**");}
 String diagnostics() throws Exception {
   StringBuilder b=new StringBuilder();
   b.append("СОСТОЯНИЕ BLUETOOTH НА ТВ\n");
   b.append("Android: ").append(clean(adb.shell("getprop ro.build.version.release"))).append("\n");
   b.append("Флаг bluetooth_on: ").append(clean(adb.shell("settings get global bluetooth_on"))).append("\n");
   String dump=adb.shell("dumpsys bluetooth_manager");
   if(dump.toLowerCase().contains("permission denial")) {
     b.append("Android не разрешил подробную диагностику.\n");
   } else {
     int count=0;
     for(String line:dump.split("\\r?\\n")) {
       String l=line.toLowerCase();
       if(l.contains("enabled:")||l.contains("state:")||l.contains("mstate")||
          l.contains("bluetooth status")||l.contains("menable:")) {
         b.append(clean(line.trim())).append("\n");
         if(++count>=12)break;
       }
     }
     if(count==0)b.append("Подробные поля состояния не обнаружены.\n");
   }
   b.append("\nАУДИО / BLUETOOTH\n");
   String audio=adb.shell("dumpsys audio | grep -i -E 'a2dp|bluetooth' | head -n 12");
   if(audio.isEmpty())b.append("Активный профиль A2DP не выявлен в краткой сводке.\n");
   else b.append(clean(audio.length()>1600?audio.substring(0,1600):audio)).append("\n");
   b.append("\nЭто чтение состояния. Сопряжения и службы не изменены.");
   return b.toString();
 }
 String settings()throws Exception{
   String out=adb.shell("am start -a android.settings.BLUETOOTH_SETTINGS");
   if(out.toLowerCase().contains("error")||out.toLowerCase().contains("exception")) {
     return "Не удалось открыть Bluetooth автоматически: "+clean(out)+
       "\nОткрой настройки Bluetooth пультом телевизора.";
   }
   return "Запрос на открытие настроек Bluetooth отправлен на ТВ.\n"+
      clean(out)+"\nЕсли открылось общее меню, найди раздел Bluetooth пультом.";
 }
 static String guide(){
   return "ПОМОЩНИК ПОДКЛЮЧЕНИЯ ЯНДЕКС СТАНЦИИ\n\n"+
     "1. Станция поддерживает только одно активное Bluetooth-соединение. "+
     "Если телефон или ноутбук подключён к Станции — отключи именно его, не пульт ТВ.\n\n"+
     "2. Скажи колонке: «Алиса, включи Bluetooth». "+
     "Проверь, что колонка вошла в режим обнаружения.\n\n"+
     "3. На телевизоре открой настройки Bluetooth, выбери Станцию в списке и подключись. "+
     "Не выключай Bluetooth на телевизоре.\n\n"+
     "4. Если снова появляется ошибка — вручную удали на телевизоре ТОЛЬКО сопряжение со Станцией "+
     "и создай пару заново. Не удаляй Bluetooth-пульт.\n\n"+
     "5. Если это не помогло — перезагрузи Станцию и телевизор и повтори попытку. "+
     "Расположи колонку ближе к телевизору.\n\n"+
     "Системные Bluetooth-службы и сохранённые сопряжения программа не сбрасывает. "+
     "Если ошибка повторяется, запусти «Диагностика Bluetooth» и сохрани результат.";
 }
}
