using System;
using System.Collections.Generic;
using UnityEngine;
using UnityEngine.InputSystem;
using UnityEngine.Rendering;
using TMPro;
using Unity.XR.CoreUtils;

public class AcrophobiaBalloonFlightController : MonoBehaviour
{
    [Header("Components")]
    public PlayerInput playerInput;
    public BioFeedbackMiddleware bioFeedback;
    public Camera flightCamera;
    public Transform cameraPivot;

    [Header("VR Settings")]
    public XROrigin xrOrigin;
    public bool recenterVRToOrigin = true;
    public float vrEyeHeight = 1.6f;
    public Camera nonVRCamera;

    [Header("Flight Parameters")]
    public float horizontalSpeed = 7f;
    public float horizontalAcceleration = 10f;
    public float horizontalDamping = 12f;
    public float minX = 200f; // Adjusted for X=375 starting point
    public float maxX = 500f; // Adjusted for X=375 starting point
    public float minZ = -500f; // Adjusted for starting at -261.32
public float maxZ = 2000f; // Increased for longer session
public bool autoForward = false; // Start stopped, wait for "start" command
    public bool loopFlightPath = false;

    [Header("Session Settings")]
    public float sessionDurationSeconds = 600f; // 10 minutes session
    public bool syncSpeedToDuration = true;
    public float forwardSpeed = 1.0f; 

    [Header("UI References")]
    public TextMeshProUGUI timeDisplay;
    public TextMeshProUGUI targetsDisplay;
    public TextMeshProUGUI heightDisplay;

    [Header("Interest & Feel")]
public float windSwayIntensity = 0.5f;
    public float windSwaySpeed = 0.4f;
    public float targetUpdateDistance = 60f;
    public Volume postProcessVolume;

    [Header("View Settings")]
    public float mouseSensitivity = 0.02f;
    public float startingYaw;
    public float startingPitch = 14f;
    public float maxViewDistance = 2000f;
    public float limitedFieldOfView = 68f;
public bool lockCursor = true;

    [Header("Collision & Targets")]
    public bool avoidBuildingCollisions = true;
    public LayerMask buildingCollisionMask = -1;
    public float collisionProbeRadius = 1.2f;
    public float collisionProbeVerticalOffset = 1.2f;
    public float targetCaptureRadius = 8f;

    private readonly List<AcrophobiaBalloonTarget> targets = new List<AcrophobiaBalloonTarget>();
private float yaw;
    private float pitch;
    private float horizontalVelocity;
    private float sessionTimer;
    private int completedCount;
    private Vector3 startPosition;
    private readonly Collider[] collisionHits = new Collider[24];
    private bool isSessionStarted = false;
    private int lastHandledBioFeedbackCommandSequence = 0;

    private InputAction moveXAction;

    private void Awake()
    {
        InitializeComponents();
        InitializeInput();
        
        yaw = startingYaw;
        pitch = startingPitch;
        startPosition = transform.position;

        ApplyCameraRotation();
        SetupCameraSettings();
        CacheTargets();

        if (syncSpeedToDuration && targets.Count > 0)
        {
            CalculateOptimalSpeed();
        }
    }

    private void Start()
    {
        if (recenterVRToOrigin && xrOrigin != null)
        {
            Invoke(nameof(RecenterVR), 0.1f);
        }
    }

    private void RecenterVR()
    {
        if (xrOrigin == null) return;
        
        // Aligns the XR camera to the balloon's center (transform.position)
        // Adjust height to user-defined eye level
        Vector3 targetWorldPos = transform.position + Vector3.up * vrEyeHeight;
        xrOrigin.MoveCameraToWorldLocation(targetWorldPos);
        
        // Apply settings again to ensure VR camera has the correct Far Clip Plane
        SetupCameraSettings();
    }

    private void CalculateOptimalSpeed()
    {
float maxTargetZ = startPosition.z;
        foreach (var t in targets)
        {
            if (t.transform.position.z > maxTargetZ)
                maxTargetZ = t.transform.position.z;
        }

        float totalDistance = maxTargetZ - startPosition.z;
        if (totalDistance > 0)
        {
            forwardSpeed = totalDistance / sessionDurationSeconds;
            Debug.Log($"Calculated Forward Speed: {forwardSpeed} (Total Distance: {totalDistance}, Duration: {sessionDurationSeconds})");
        }
    }

    private void InitializeComponents()
    {
        if (playerInput == null) playerInput = GetComponent<PlayerInput>();
        if (bioFeedback == null) bioFeedback = GetComponent<BioFeedbackMiddleware>();
        if (postProcessVolume == null) postProcessVolume = UnityEngine.Object.FindAnyObjectByType<Volume>();
        
        SetupCameras();
    }

    private void SetupCameras()
    {
        bool isVR = UnityEngine.XR.Management.XRGeneralSettings.Instance?.Manager?.activeLoader != null;
        
        if (xrOrigin == null) xrOrigin = GetComponentInChildren<XROrigin>(true);
        if (nonVRCamera == null)
        {
            var camTrans = transform.Find("Balloon First Person Camera");
            if (camTrans != null) nonVRCamera = camTrans.GetComponent<Camera>();
        }

        if (isVR && xrOrigin != null)
        {
            if (nonVRCamera != null) nonVRCamera.gameObject.SetActive(false);
            xrOrigin.gameObject.SetActive(true);
            flightCamera = xrOrigin.Camera;
            cameraPivot = (flightCamera != null) ? flightCamera.transform : null;
        }
        else if (nonVRCamera != null)
        {
            if (xrOrigin != null) xrOrigin.gameObject.SetActive(false);
            nonVRCamera.gameObject.SetActive(true);
            flightCamera = nonVRCamera;
            cameraPivot = flightCamera.transform;
        }

        // Fallback for references
        if (flightCamera == null) flightCamera = GetComponentInChildren<Camera>(true);
        if (cameraPivot == null && flightCamera != null) cameraPivot = flightCamera.transform;

        SetupCameraSettings();
    }

    private void InitializeInput()
    {
        if (playerInput != null)
        {
            // Restricted to X-axis only as per requirements
            moveXAction = playerInput.actions.FindAction("MoveX");
        }

        bool isVR = UnityEngine.XR.XRSettings.enabled || UnityEngine.XR.Management.XRGeneralSettings.Instance?.Manager?.activeLoader != null;
        if (lockCursor && !isVR)
        {
            Cursor.lockState = CursorLockMode.Locked;
            Cursor.visible = false;
        }
    }

    private void Update()
    {
        HandleMiddlewareCommands();
        
        if (isSessionStarted)
        {
            UpdateSessionTimer();
            HandleFlightMovement();
            SendHeightTelemetry();
        }
        
        CheckTargets();
        UpdateActiveTargetHeight();
        UpdateUI();
        ApplyDynamicEffects();
    }

    private void HandleMiddlewareCommands()
    {
        if (bioFeedback == null) return;

        bioFeedback.GetLatestCommandSnapshot(out string cmd, out int sequence);
        if (sequence == lastHandledBioFeedbackCommandSequence) return;

        lastHandledBioFeedbackCommandSequence = sequence;

        // "start" command from Python app
        if (cmd == "start" && !isSessionStarted)
        {
            isSessionStarted = true;
            autoForward = true;
            Debug.Log($"Start command received. isSessionStarted: {isSessionStarted}, autoForward: {autoForward}");
        }
        // "stop" command from Python app
        else if (cmd == "stop")
        {
            ResetSession(resetAltitude: true);
            Debug.Log("Biofeedback command: stop flight and reset session");
        }
    }

    private void UpdateSessionTimer()
    {
        sessionTimer += Time.deltaTime;
        if (sessionTimer >= sessionDurationSeconds)
        {
            if (loopFlightPath) 
            {
                ResetSession();
            }
            else 
            {
                isSessionStarted = false;
                autoForward = false;
                Debug.Log("Session Finished");
            }
        }
    }

    private void HandleFlightMovement()
    {
        float deltaTime = Time.deltaTime;
        
        // Horizontal Control (X Only)
        float horizontalInput = moveXAction?.ReadValue<float>() ?? 0f;
        float targetHorizontalVel = horizontalInput * horizontalSpeed;
        float horizontalStep = Mathf.Abs(horizontalInput) > 0.01f ? horizontalAcceleration : horizontalDamping;
        horizontalVelocity = Mathf.MoveTowards(horizontalVelocity, targetHorizontalVel, horizontalStep * deltaTime);

        Vector3 position = transform.position;
        
        // Altitude: Controlled by Middleware signals ("increase"/"decrease")
        if (bioFeedback != null)
        {
            position.y = bioFeedback.CurrentAltitude;
        }

        // Apply Horizontal movement with sway
        float windSway = Mathf.Sin(Time.time * windSwaySpeed) * windSwayIntensity;
        Vector3 nextLateralPos = position;
        nextLateralPos.x = Mathf.Clamp(nextLateralPos.x + (horizontalVelocity + windSway) * deltaTime, minX, maxX);

        if (!IsPositionBlocked(nextLateralPos))
        {
            position.x = nextLateralPos.x;
        }
        else
        {
            horizontalVelocity = 0f;
        }

        // Forward Movement along Z axis
        if (autoForward)
        {
            Vector3 nextForwardPos = position;
            nextForwardPos.z += forwardSpeed * deltaTime;

            if (nextForwardPos.z > maxZ)
            {
                if (loopFlightPath)
                {
                    ResetFlightPath();
                    return;
                }
                nextForwardPos.z = maxZ;
            }

            if (!IsPositionBlocked(nextForwardPos))
            {
                position.z = nextForwardPos.z;
            }
        }

        position.z = Mathf.Clamp(position.z, minZ, maxZ);
        transform.position = position;
    }

    private void SendHeightTelemetry()
    {
        if (bioFeedback == null) return;

        bioFeedback.SendHeightTelemetry(transform.position.y);
    }

    private void SetupCameraSettings()
    {
        if (flightCamera == null) return;
        flightCamera.farClipPlane = maxViewDistance;
        flightCamera.nearClipPlane = 0.03f;
        flightCamera.fieldOfView = limitedFieldOfView;
    }

    private void ApplyCameraRotation()
    {
        if (cameraPivot != null)
        {
            cameraPivot.localRotation = Quaternion.Euler(pitch, yaw, 0f);
        }
    }

    private void UpdateActiveTargetHeight()
    {
        // Find the first uncompleted target that is still ahead of the balloon
        AcrophobiaBalloonTarget nextTarget = targets.Find(t => !t.Completed && t.transform.position.z > transform.position.z);
        if (nextTarget == null) return;

        float zDist = nextTarget.transform.position.z - transform.position.z;
        if (zDist < targetUpdateDistance)
        {
            nextTarget.UpdateTargetAltitude(transform.position.y);
        }
    }

    private void ApplyDynamicEffects()
    {
        if (postProcessVolume != null && bioFeedback != null)
        {
            if (postProcessVolume.profile.TryGet<UnityEngine.Rendering.Universal.Bloom>(out var bloom))
            {
                float t = Mathf.InverseLerp(bioFeedback.minAltitude, bioFeedback.maxAltitude, transform.position.y);
                bloom.intensity.value = Mathf.Lerp(1f, 4f, t);
            }
        }
    }

    private void UpdateUI()
    {
        if (timeDisplay != null)
        {
            float remaining = Mathf.Max(0, sessionDurationSeconds - sessionTimer);
            timeDisplay.text = $"Time Left: {(int)remaining / 60:00}:{(int)remaining % 60:00}";
        }

        if (targetsDisplay != null)
        {
            targetsDisplay.text = $"Score: {completedCount}/{targets.Count}";
        }

        if (heightDisplay != null)
        {
            heightDisplay.text = $"Height: {transform.position.y:F1}m";
        }
    }

    private bool IsPositionBlocked(Vector3 position)
    {
        Vector3 probePosition = position + Vector3.up * collisionProbeVerticalOffset;
        int hitCount = Physics.OverlapSphereNonAlloc(probePosition, collisionProbeRadius, collisionHits, -1, QueryTriggerInteraction.Ignore);

        for (int i = 0; i < hitCount; i++)
        {
            Collider hit = collisionHits[i];
            collisionHits[i] = null;
            if (hit != null && !hit.transform.IsChildOf(transform)) 
            {
                Debug.LogWarning($"Movement blocked by: {hit.name} (Layer: {LayerMask.LayerToName(hit.gameObject.layer)}) at {hit.transform.position}. Balloon position: {transform.position}");
                return true;
            }
        }
        return false;
    }

    private void ResetSession(bool resetAltitude = false)
    {
        sessionTimer = 0f;
        completedCount = 0;
        isSessionStarted = false;
        autoForward = false;
        if (resetAltitude && bioFeedback != null) bioFeedback.ResetAltitude();
        ResetTargets();
        ResetFlightPath();
        if (syncSpeedToDuration) CalculateOptimalSpeed();
    }

    private void ResetFlightPath()
    {
        Vector3 resetPos = startPosition;
        if (bioFeedback != null) resetPos.y = bioFeedback.CurrentAltitude;
        resetPos.x = Mathf.Clamp(resetPos.x, minX, maxX);
        resetPos.z = Mathf.Clamp(resetPos.z, minZ, maxZ);
        transform.position = resetPos;
        horizontalVelocity = 0f;
    }

    private void CacheTargets()
    {
        targets.Clear();
        AcrophobiaBalloonTarget[] foundTargets = UnityEngine.Object.FindObjectsByType<AcrophobiaBalloonTarget>(FindObjectsSortMode.None);
        targets.AddRange(foundTargets);
        targets.Sort((left, right) => left.pathOrder.CompareTo(right.pathOrder));
    }

    private void CheckTargets()
    {
        completedCount = 0;
        foreach (var target in targets)
        {
            if (target.Completed)
            {
                completedCount++;
                continue;
            }

            if (Vector3.Distance(transform.position, target.transform.position) <= target.captureRadius)
            {
                target.SetCompleted();
                completedCount++;
            }
        }
    }

    private void ResetTargets()
    {
        completedCount = 0;
        foreach (var t in targets)
        {
            if (t != null) t.ResetTarget();
        }
    }
}
