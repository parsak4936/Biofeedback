using UnityEngine;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using System.Globalization;

public class BioFeedbackMiddleware : MonoBehaviour
{
    [Header("Network Settings")]
    public int listenPort = 5005;

    [Header("Telemetry Placeholder")]
    public bool enableHeightTelemetry = false;
    public string telemetryHost = "127.0.0.1";
    public int telemetryPort = 5006;
    public float telemetryRateHz = 10f;
    
    [Header("Altitude Mapping")]
    public float minAltitude = 0f; // Baseline ground
    public float maxAltitude = 150f; // Requested max
    public float stepAmount = 1f; 
    public float verticalSpeed = 1.0f;

    private float targetAltitude;
    private float currentAltitude;
    private float _altitudeLogTimer = 0f;
    private string lastReceivedCommand = "";
    private UdpClient udpClient;
    private UdpClient telemetryClient;
    private Thread receiveThread;
    private readonly object lockObject = new object();
    private bool isRunning;
    private bool hasNewCommand = false;
    private int commandSequence = 0;
    private float lastTelemetrySendTime = -999f;

    // Public properties for the Flight Controller to read
    public float CurrentAltitude => currentAltitude;
    public string LastCommand
    {
        get
        {
            lock (lockObject) return lastReceivedCommand;
        }
    }

    public int CommandSequence
    {
        get
        {
            lock (lockObject) return commandSequence;
        }
    }

    public void GetLatestCommandSnapshot(out string command, out int sequence)
    {
        lock (lockObject)
        {
            command = lastReceivedCommand;
            sequence = commandSequence;
        }
    }

    public void ResetAltitude()
    {
        targetAltitude = minAltitude;
        currentAltitude = minAltitude;
    }

    public void SendHeightTelemetry(float heightMeters)
    {
        if (!enableHeightTelemetry) return;

        float minInterval = telemetryRateHz > 0f ? 1f / telemetryRateHz : 0f;
        if (Time.time - lastTelemetrySendTime < minInterval) return;

        lastTelemetrySendTime = Time.time;
        if (telemetryClient == null) telemetryClient = new UdpClient();

        string payload = string.Format(
            CultureInfo.InvariantCulture,
            "height,{0:F3}",
            heightMeters
        );

        try
        {
            byte[] data = Encoding.UTF8.GetBytes(payload);
            telemetryClient.Send(data, data.Length, telemetryHost, telemetryPort);
        }
        catch (System.Exception e)
        {
            Debug.LogWarning($"Height telemetry send failed: {e.Message}");
        }
    }

    private void Awake()
    {
        currentAltitude = minAltitude; 
        targetAltitude = minAltitude;
        StartListening();
    }

    private void OnDestroy()
    {
        StopListening();
    }

    private void StartListening()
    {
        if (receiveThread != null) return;

        isRunning = true;
        receiveThread = new Thread(ReceiveData);
        receiveThread.IsBackground = true;
        receiveThread.Start();
        Debug.Log($"UDP Listener started on port {listenPort}");
    }

    private void StopListening()
    {
        isRunning = false;
        if (udpClient != null)
        {
            udpClient.Close();
            udpClient = null;
        }
        if (receiveThread != null)
        {
            receiveThread.Join(500);
            receiveThread = null;
        }
        if (telemetryClient != null)
        {
            telemetryClient.Close();
            telemetryClient = null;
        }
    }

    private void ReceiveData()
    {
        try
        {
            udpClient = new UdpClient(listenPort);
            IPEndPoint remoteEndPoint = new IPEndPoint(IPAddress.Any, 0);

            while (isRunning)
            {
                byte[] data = udpClient.Receive(ref remoteEndPoint);
                string message = Encoding.UTF8.GetString(data).Trim().ToLower();

                lock (lockObject)
                {
                    lastReceivedCommand = message;
                    hasNewCommand = true;
                    commandSequence++;
                }
            }
        }
        catch (System.Exception e)
        {
            if (isRunning) Debug.LogError($"UDP Receive Error: {e.Message}");
        }
    }

    [Header("Testing")]
    public bool enableKeyboardSimulation = true;

    private void Update()
    {
        if (enableKeyboardSimulation)
        {
            if (Input.GetKey(KeyCode.UpArrow)) 
                targetAltitude = Mathf.Min(targetAltitude + 5.0f * Time.deltaTime, maxAltitude);
            if (Input.GetKey(KeyCode.DownArrow)) 
                targetAltitude = Mathf.Max(targetAltitude - 5.0f * Time.deltaTime, minAltitude);
            
            if (Input.GetKeyDown(KeyCode.Space)) SetCommand("start");
            if (Input.GetKeyDown(KeyCode.S)) SetCommand("stop");
        }

        ProcessCommands();
        
        // Smoothly move towards the target altitude
        currentAltitude = Mathf.MoveTowards(currentAltitude, targetAltitude, verticalSpeed * Time.deltaTime);

        _altitudeLogTimer += Time.deltaTime;
        if (_altitudeLogTimer >= 1.0f)
        {
            _altitudeLogTimer = 0f;
            Debug.Log($"Altitude - Current: {currentAltitude:F2}m, Target: {targetAltitude}m");
        }
    }

    private void ProcessCommands()
    {
        string cmd;
        bool isNew;

        lock (lockObject)
        {
            cmd = lastReceivedCommand;
            isNew = hasNewCommand;
            hasNewCommand = false; // Reset the flag after reading
        }

        // Each time an "increase" or "decrease" packet is received, we adjust the target altitude by 1m
        if (isNew)
        {
            if (cmd == "increase")
            {
                targetAltitude = Mathf.Min(targetAltitude + stepAmount, maxAltitude);
                Debug.Log($"Command Received: {cmd}. New Target Altitude: {targetAltitude}m");
            }
            else if (cmd == "decrease")
            {
                targetAltitude = Mathf.Max(targetAltitude - stepAmount, minAltitude);
                Debug.Log($"Command Received: {cmd}. New Target Altitude: {targetAltitude}m");
            }
        }
    }

    // Manual setter for testing
    public void SetCommand(string command)
    {
        lock (lockObject)
        {
            lastReceivedCommand = command.ToLower();
            hasNewCommand = true;
            commandSequence++;
        }
    }
}
