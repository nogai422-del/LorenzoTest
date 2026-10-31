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

 /** Store sanitized snapshots on the phone; never reset bonds or collect MAC addresses. */
 String saveSnapshot(android.content.SharedPreferences prefs,String ip,boolean failed)throws Exception{
   String label=failed?"failure":"success";
   String report=diagnostics();
   String at=new java.text.SimpleDateFormat("yyyy-MM-dd HH:mm",java.util.Locale.getDefault()).format(new java.util.Date());
   prefs.edit().putString("max_"+ip+"_"+label,at+"\\n"+report).apply();
   return "Снимок '"+(failed?"во время ошибки":"после успешного подключения")+"' сохранён на телефоне.\\n\\n"+report;
 }
 String compare(android.content.SharedPreferences prefs,String ip){
   String failure=prefs.getString("max_"+ip+"_failure",null);
   String success=prefs.getString("max_"+ip+"_success",null);
   if(failure==null||success==null)return "Чтобы сравнить, сделай оба снимка: при ошибке и после успешного подключения. Снимки не изменяют Bluetooth.";
   StringBuilder b=new StringBuilder("СРАВНЕНИЕ BLUETOOTH СТАНЦИИ МАКС\\n\\n");
   java.util.LinkedHashSet<String> a=lines(failure),c=lines(success);
   b.append("Только при ошибке:\\n");
   for(String x:a)if(!c.contains(x))b.append(x).append("\\n");
   b.append("\\nТолько при успешном подключении:\\n");
   for(String x:c)if(!a.contains(x))b.append(x).append("\\n");
   b.append("\\nЗаметка: A2DP и состояние аудио можно отличить по строкам в отчёте. Разница не устанавливает причину автоматически. Bluetooth-пульт и все сопряжения сохранены.");
   return b.toString();
 }
 private static java.util.LinkedHashSet<String> lines(String value){
   java.util.LinkedHashSet<String> set=new java.util.LinkedHashSet<>();
   for(String l:value.split("\\r?\\n")){
     String s=l.trim();
     if(s.length()>0&&!s.startsWith("СОСТОЯНИЕ")&&!s.startsWith("АУДИО /")&&!s.startsWith("Android:")&&!s.startsWith("Это чтение"))set.add(s);
   }
   return set;
 }
 static String maxGuide(String problem){
   String common="\\n\\nНЕ МЕНЯЙ: Bluetooth ON в Factory Menu, системные пакеты Bluetooth и привязку пульта. "+
    "Не запускай полный сброс телевизора. Если нужна новая пара, удаляй вручную только 'Яндекс Станция Макс'.";
   if("A".equals(problem))return "СТАНЦИЯ МАКС: ОБНАРУЖЕНА, НО НЕ ПОДКЛЮЧАЕТСЯ\\n"+
    "1. Отключи от Станции телефон, ноутбук и другие источники Bluetooth — допускается только одно активное соединение.\\n"+
    "2. Скажи: «Алиса, включи Bluetooth».\\n"+
    "3. Открой Bluetooth на ТВ и выбери Станцию Макс.\\n"+
    "4. Если ошибка повторится, сделай «Снимок во время сбоя». Лишь затем попробуй удалить на ТВ сопряжение со Станцией и создать его заново."+common;
   if("B".equals(problem))return "СТАНЦИЯ МАКС: ОТВАЛИВАЕТСЯ ПОСЛЕ ПОДКЛЮЧЕНИЯ\\n"+
    "1. Убедись, что другое устройство не перехватывает Станцию по Bluetooth.\\n"+
    "2. Поставь колонку близко к телевизору и убери препятствия.\\n"+
    "3. Проверь, работает ли звук и какой выход выбран на телевизоре.\\n"+
    "4. Сохрани снимок сразу после успешного соединения и снимок после обрыва; затем сравни их.\\n"+
    "5. Если обрывы повторяются, обнови прошивки ТВ и Станции штатным способом."+common;
   return "СТАНЦИЯ МАКС: НЕ ПОДКЛЮЧАЕТСЯ ПОСЛЕ ВКЛЮЧЕНИЯ ТВ\\n"+
    "1. Яндекс предупреждает: автоматическое переподключение после перезагрузки не гарантируется.\\n"+
    "2. Скажи: «Алиса, включи Bluetooth».\\n"+
    "3. На ТВ выбери ранее сопряжённую Станцию и нажми «Подключить».\\n"+
    "4. Если это не помогает, только сопряжение Станции можно удалить вручную и создать заново.\\n"+
    "5. Для постоянных отказов сохрани снимок состояния Bluetooth."+common;
 }

}
