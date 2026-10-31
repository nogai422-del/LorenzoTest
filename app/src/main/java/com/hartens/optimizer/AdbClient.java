package com.hartens.optimizer;
import android.content.*;
import android.util.Base64;
import java.io.*;
import java.net.*;
import java.nio.*;
import java.nio.charset.StandardCharsets;
import java.math.BigInteger;
import java.security.*;
import java.security.interfaces.RSAPublicKey;
import java.security.spec.*;
public final class AdbClient implements AutoCloseable {
 private static final int CNXN=id("CNXN"),AUTH=id("AUTH"),OPEN=id("OPEN"),OKAY=id("OKAY"),WRTE=id("WRTE"),CLSE=id("CLSE"),STLS=id("STLS");
 private final Context ctx; private Socket sock; private InputStream in; private OutputStream out; private int sid=1;
 AdbClient(Context c){ctx=c.getApplicationContext();}
 private static int id(String s){byte[] b=s.getBytes(StandardCharsets.US_ASCII);return (b[0]&255)|((b[1]&255)<<8)|((b[2]&255)<<16)|((b[3]&255)<<24);}
 private static byte[] bytes(String s){return s.getBytes(StandardCharsets.UTF_8);}
 private static final class P {int cmd,a,b;byte[] data;}
 private void send(int cmd,int a,int b,byte[] d)throws Exception {int sum=0;for(byte x:d)sum+=x&255;ByteBuffer h=ByteBuffer.allocate(24).order(ByteOrder.LITTLE_ENDIAN);h.putInt(cmd).putInt(a).putInt(b).putInt(d.length).putInt(sum).putInt(cmd^(-1));out.write(h.array());out.write(d);out.flush();}
 private P recv()throws Exception {byte[] h=new byte[24];readAll(h);ByteBuffer bb=ByteBuffer.wrap(h).order(ByteOrder.LITTLE_ENDIAN);P p=new P();p.cmd=bb.getInt();p.a=bb.getInt();p.b=bb.getInt();int n=bb.getInt(),sum=bb.getInt(),magic=bb.getInt();if(n<0||n>1048576||magic!=(p.cmd^(-1)))throw new IOException("Invalid ADB header");p.data=new byte[n];readAll(p.data);int cs=0;for(byte x:p.data)cs+=x&255;if(cs!=sum)throw new IOException("ADB checksum error");return p;}
 private void readAll(byte[] a)throws Exception {int i=0;while(i<a.length){int n=in.read(a,i,a.length-i);if(n<0)throw new EOFException("Телевизор закрыл соединение");i+=n;}}
 private KeyPair key()throws Exception{SharedPreferences p=ctx.getSharedPreferences("auth",0);String s=p.getString("priv",null),t=p.getString("pub",null);KeyFactory f=KeyFactory.getInstance("RSA");if(s!=null&&t!=null)try{return new KeyPair(f.generatePublic(new X509EncodedKeySpec(Base64.decode(t,0))),f.generatePrivate(new PKCS8EncodedKeySpec(Base64.decode(s,0))));}catch(Exception ignored){}KeyPairGenerator g=KeyPairGenerator.getInstance("RSA");g.initialize(2048);KeyPair k=g.generateKeyPair();p.edit().putString("priv",Base64.encodeToString(k.getPrivate().getEncoded(),2)).putString("pub",Base64.encodeToString(k.getPublic().getEncoded(),2)).apply();return k;}
 private static byte[] le(BigInteger n,int len){byte[] b=n.toByteArray(),v=new byte[len];for(int i=0;i<len&&i<b.length;i++)v[i]=b[b.length-1-i];return v;}
 private static byte[] pub(KeyPair k){RSAPublicKey r=(RSAPublicKey)k.getPublic();BigInteger n=r.getModulus(),base=BigInteger.ONE.shiftLeft(32);ByteBuffer b=ByteBuffer.allocate(524).order(ByteOrder.LITTLE_ENDIAN);b.putInt(64).putInt(base.subtract(n.mod(base).modInverse(base)).intValue());b.put(le(n,256));b.put(le(BigInteger.ONE.shiftLeft(4096).mod(n),256));b.putInt(r.getPublicExponent().intValue());return bytes(Base64.encodeToString(b.array(),2)+" hartens-optimizer@phone\u0000");}
 private static byte[] signature(KeyPair k,byte[] token)throws Exception{if(token.length!=20)throw new IOException("Invalid ADB token");byte[] prefix={0x30,0x21,0x30,0x09,0x06,0x05,0x2b,0x0e,0x03,0x02,0x1a,0x05,0x00,0x04,0x14};Signature s=Signature.getInstance("NONEwithRSA");s.initSign(k.getPrivate());s.update(prefix);s.update(token);return s.sign();}
 void connect(String ip)throws Exception {if(!ip.matches("[0-9]{1,3}(\\.[0-9]{1,3}){3}"))throw new IOException("Неверный IP");for(String bit:ip.split("\\."))if(Integer.parseInt(bit)>255)throw new IOException("Неверный IP");sock=new Socket();sock.connect(new InetSocketAddress(ip,5555),5000);sock.setSoTimeout(18000);in=sock.getInputStream();out=sock.getOutputStream();KeyPair k=key();send(CNXN,0x01000000,4096,bytes("host::\u0000"));int phase=0;for(int i=0;i<7;i++){P p=recv();if(p.cmd==CNXN)return;if(p.cmd==STLS)throw new IOException("TLS ADB не поддерживается");if(p.cmd==AUTH&&p.a==1){if(phase==0)send(AUTH,2,0,signature(k,p.data));else if(phase==1)send(AUTH,3,0,pub(k));else throw new IOException("Подтверди разрешение ADB на ТВ");phase++;continue;}throw new IOException("Ошибка согласования ADB");}throw new IOException("ADB не авторизован");}
 String shell(String cmd)throws Exception{int local=sid++,remote=-1;send(OPEN,local,0,bytes("shell:"+cmd+"\u0000"));ByteArrayOutputStream b=new ByteArrayOutputStream();for(int i=0;i<1000;i++){P p=recv();if(p.cmd==OKAY&&p.b==local){remote=p.a;continue;}if(p.cmd==WRTE&&p.b==local){remote=p.a;if(b.size()<100000)b.write(p.data,0,Math.min(p.data.length,100000-b.size()));send(OKAY,local,remote,new byte[0]);continue;}if(p.cmd==CLSE&&p.b==local){send(CLSE,local,p.a,new byte[0]);return b.toString("UTF-8").trim();}}throw new IOException("ADB timeout");}
 public void close(){try{if(sock!=null)sock.close();}catch(Exception ignored){}}
}
